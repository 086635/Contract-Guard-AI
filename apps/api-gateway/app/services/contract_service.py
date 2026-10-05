"""合同上传 Service。

设计要点：
1. presign 只生成对象键和 PUT URL，不中转文件内容。
2. upload 从对象存储读取真实大小/ETag，再写合同与任务。
3. 网关只派发 `parse_doc`，不执行解析逻辑。

对应：`文件集合/codex/4.C10上传直传与解析派发.md`。
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db.repository.auth import UserRecord
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
    owner_scope,
)
from app.db.repository.result import (
    ElementRepository,
    ReportRepository,
    RiskRepository,
    SealRepository,
)
from app.generated import schemas as s
from app.generated.schemas import (
    ContractStatus,
    FileType,
    PresignData,
    TaskProgress,
    UploadBody,
)
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.ingestion import IngestionService
from app.services.projector import ContractProjector
from app.services.storage import UploadObjectError, validate_upload_object
from app.services.task_service import TaskService
from rag_contracts.enums import ContractType

#: 单文件大小上限（唯一处；C56 §⑥ 沿用既有 50 MB 约定）。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: 声明文件类型 → 允许的扩展名（**服务端准入唯一处**，C56 §⑥）。
#: 扩展名只用于「声明的类型与文件名是否自洽」；真实内容判据是 C57 的 magic sniff。
_ALLOWED_EXTENSIONS: dict[FileType, frozenset[str]] = {
    FileType.pdf_native: frozenset({".pdf"}),
    FileType.pdf_scanned: frozenset({".pdf"}),
    FileType.docx: frozenset({".docx"}),
    # T-D8-2：图片输入（真实类型仍由 C57 魔数判定，扩展名只做自洽校验）
    FileType.image: frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"}),
}


class PresignRequest(BaseModel):
    """上传 presign 请求体（C56 兼容新增 `sizeBytes` / `contentType`）。"""

    model_config = ConfigDict(populate_by_name=True)

    file_name: str = Field(..., alias="fileName")
    file_type: FileType = Field(..., alias="fileType")
    size_bytes: int | None = Field(None, alias="sizeBytes", ge=1)
    content_type: str | None = Field(None, alias="contentType")


class ContractService:
    """合同上传与解析派发。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._contracts = ContractRepository(context.session)
        self._tasks = TaskRepository(context.session)
        self._elements = ElementRepository(context.session)
        self._risks = RiskRepository(context.session)
        self._seals = SealRepository(context.session)
        self._reports = ReportRepository(context.session)

    async def presign_upload(self, body: PresignRequest) -> PresignData:
        """校验声明并生成对象键和 PUT 预签名 URL（C56）。"""
        _validate_presign_request(body)
        file_key = generate_file_key(body.file_name)
        url = self._context.object_storage.presign_put(file_key, 600)
        return PresignData(
            presignedUrl=url,
            fileKey=file_key,
            expiresIn=600,
            contentType=body.content_type,
        )

    async def upload(
        self,
        body: UploadBody,
        user: UserRecord | None = None,
    ) -> TaskProgress:
        """复核对象后创建合同/任务并派发解析（C56）。"""
        # 先复核 key 形态再触碰对象存储：非法 key 不得触发 bucket 访问。
        validate_upload_object(body.file_key)
        object_info = self._context.object_storage.stat_object(body.file_key)
        validate_upload_object(
            body.file_key,
            object_info,
            expected_size=body.size_bytes,
            expected_etag=body.etag,
            max_bytes=MAX_UPLOAD_BYTES,
        )
        file_hash = object_info.etag.strip('"')[:64]
        if not file_hash:
            file_hash = hashlib.sha256(body.file_key.encode("utf-8")).hexdigest()

        # C81：同一文件（同哈希）重复上传 → 友好 409，而不是让唯一约束冒泡成 500。
        #   `contracts_file_hash_key` 是既有去重约束（E2E 靠每次改字节规避）。
        if await self._contracts.get_by_file_hash(file_hash) is not None:
            raise UploadObjectError(
                409,
                "ERR_FILE_ALREADY_UPLOADED：该文件此前已上传过（平台按文件哈希去重），"
                "请改用其它文件或修改文件后重传",
            )
        try:
            contract = await self._contracts.create(
                ContractCreate(
                    filename=body.file_name,
                    file_size=max(0, object_info.size),
                    file_hash=file_hash,
                    storage_path=body.file_key,
                    status=ContractStatus.parsing,
                    created_by_id=user.id if user is not None else None,
                    legacy=False,
                )
            )
        except IntegrityError as exc:
            # 并发下同哈希同时插入：兜底映射成同一个 409。
            raise UploadObjectError(
                409,
                "ERR_FILE_ALREADY_UPLOADED：该文件此前已上传过（平台按文件哈希去重），"
                "请改用其它文件或修改文件后重传",
            ) from exc
        task_kind, task_args = _dispatch_target(body, str(contract.id))
        # C62：**派发前**先落可恢复任务行；派发失败立即置终态，避免留下幽灵 RUNNING。
        task = await self._tasks.create(contract.id, task_kind.name, None)
        try:
            celery_task_id = self._context.task_dispatcher.send(
                task_kind,
                args=task_args,
            )
        except Exception as exc:
            await self._tasks.mark_failed(
                task.id,
                error_code="DISPATCH_FAILED",
                error_message=type(exc).__name__,
            )
            await self._context.session.commit()
            raise
        task = await self._tasks.attach_celery_task_id(task.id, celery_task_id)
        await self._context.session.commit()
        return TaskProgress(
            taskId=str(task.id),
            stage=task.stage,
            percent=task.progress,
            status=task.status,
            result={"contractId": str(contract.id)},
        )

    async def retry_parse(self, contract_id: UUID, user: UserRecord) -> TaskProgress:
        """Reprocess the stored file; serialize concurrent retries on its contract row."""
        owner_id, include_all = owner_scope(user)
        contract = await self._contracts.get(
            contract_id, owner_id=owner_id, include_all=include_all, for_update=True
        )
        if contract is None:
            raise UploadObjectError(404, "contract not found")
        if contract.status == ContractStatus.finalized:
            raise UploadObjectError(409, "已定稿合同不能重新解析，请上传新的版本")
        body = UploadBody(
            fileName=contract.filename,
            fileKey=contract.storage_path,
            fileType=ContractProjector._file_type(contract.filename),
            sizeBytes=contract.file_size,
        )
        task_kind, task_args = _dispatch_target(body, str(contract_id))
        if settings.upload_av_mode == "clamav":
            await IngestionService(self._context).link_scanned_parse(contract_id)
        for pending_kind in dict.fromkeys((task_kind, TaskKind.PARSE_DOC)):
            latest = await self._tasks.get_latest_by_contract(
                contract_id, pending_kind.name
            )
            if latest is not None:
                current = await TaskService(self._context).get(latest.id)
                if current is not None and current.status == s.Status.running:
                    await self._context.session.commit()
                    return current
        audit = await self._tasks.get_latest_by_contract(
            contract_id, TaskKind.RUN_AUDIT.name
        )
        if audit is not None:
            current_audit = await TaskService(self._context).get(audit.id)
            if current_audit is not None and current_audit.status == s.Status.running:
                raise UploadObjectError(409, "审核任务正在运行，请完成后再重新解析")
        info = self._context.object_storage.stat_object(contract.storage_path)
        validate_upload_object(
            contract.storage_path,
            info,
            expected_size=contract.file_size,
            max_bytes=MAX_UPLOAD_BYTES,
        )
        task = await self._tasks.create(contract_id, task_kind.name, None)
        try:
            celery_id = self._context.task_dispatcher.send(task_kind, args=task_args)
        except Exception as exc:
            await self._tasks.mark_failed(
                task.id, error_code="DISPATCH_FAILED", error_message=type(exc).__name__
            )
            await self._context.session.commit()
            raise
        task = await self._tasks.attach_celery_task_id(task.id, celery_id)
        await self._contracts.update_status(contract_id, ContractStatus.parsing)
        await self._context.session.commit()
        return TaskProgress(
            taskId=str(task.id),
            stage=task.stage,
            percent=task.progress,
            status=task.status,
            result={"contractId": str(contract_id)},
        )

    async def list(
        self,
        page: int,
        page_size: int,
        *,
        contract_type: str | None = None,
        status: str | None = None,
        user: UserRecord | None = None,
    ) -> tuple[list[s.ContractSummary], int]:
        """分页聚合合同摘要。"""
        parsed_type = ContractType(contract_type) if contract_type is not None else None
        parsed_status = s.ContractStatus(status) if status is not None else None
        owner_id, include_all = owner_scope(user)
        details, total = await self._contracts.list(
            page,
            page_size,
            contract_type=parsed_type,
            status=parsed_status,
            owner_id=owner_id,
            include_all=include_all,
        )
        summaries = []
        for detail in details:
            risks = await self._risks.list_by_contract(detail.id)
            conflicts = await self._risks.list_conflicts_by_contract(detail.id)
            summaries.append(ContractProjector.summary(detail, risks, conflicts))
        return summaries, total

    async def get(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> s.Contract | None:
        """聚合单合同完整详情。"""
        owner_id, include_all = owner_scope(user)
        detail = await self._contracts.get(
            contract_id,
            owner_id=owner_id,
            include_all=include_all,
        )
        if detail is None:
            return None
        if await IngestionService(self._context).finalize_audit(contract_id):
            detail = await self._contracts.get(
                contract_id, owner_id=owner_id, include_all=include_all
            )
            if detail is None:
                return None
        elements = await self._elements.list_by_contract(contract_id)
        risks = await self._risks.list_by_contract(contract_id)
        conflicts = await self._risks.list_conflicts_by_contract(contract_id)
        seals = await self._seals.list_by_contract(contract_id)
        report = await self._reports.get(contract_id)
        return ContractProjector.contract(
            detail,
            elements=elements,
            risks=risks,
            conflicts=conflicts,
            seals=seals,
            report=report,
        )


def _dispatch_target(
    body: UploadBody,
    contract_id: str,
) -> tuple[TaskKind, tuple[object, ...]]:
    """按 `CG_UPLOAD_AV_MODE` 决定首跳任务（C57 §⑤.8）。

    - `off`（development/test 默认）：保持原有行为，直接派发 `parse_doc`。
    - `clamav`：先派发 `scan_upload`；**扫描通过后由 worker 链式派发 `parse_doc`**，
      扫描未通过绝不进入解析。
    """
    if settings.upload_av_mode == "clamav":
        return (
            TaskKind.SCAN_UPLOAD,
            (
                body.file_key,
                body.file_name,
                body.file_type.value,
                contract_id,
            ),
        )
    return TaskKind.PARSE_DOC, (body.file_key, contract_id)


def _validate_presign_request(body: PresignRequest) -> None:
    """presign 准入：扩展名与声明类型自洽、大小上限、production 必填大小（C56 §⑥）。"""
    allowed = _ALLOWED_EXTENSIONS.get(body.file_type)
    suffix = Path(body.file_name).suffix.lower()
    if allowed is None or suffix not in allowed:
        raise UploadObjectError(
            415,
            "ERR_UNSUPPORTED_FILE_TYPE：仅支持 PDF / DOCX，且扩展名需与 fileType 一致",
        )
    if body.size_bytes is None:
        if settings.runtime_env == "production":
            raise UploadObjectError(
                400, "ERR_VALIDATION：production 必须提供 sizeBytes"
            )
        return
    if body.size_bytes > MAX_UPLOAD_BYTES:
        raise UploadObjectError(413, "ERR_FILE_TOO_LARGE：单文件不得超过 50 MB")


def generate_file_key(
    file_name: str,
    *,
    now: datetime | None = None,
) -> str:
    """生成安全、唯一的 MinIO 对象键。"""
    timestamp = now or datetime.now(UTC)
    safe_name = _safe_filename(file_name)
    return (
        f"contracts/{timestamp.year:04d}/{timestamp.month:02d}/"
        f"{uuid4().hex}-{safe_name}"
    )


def _safe_filename(file_name: str) -> str:
    basename = Path(file_name).name
    if not basename:
        raise ValueError("fileName 不能为空")
    return re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]", "_", basename)


__all__ = [
    "MAX_UPLOAD_BYTES",
    "ContractService",
    "PresignRequest",
    "generate_file_key",
]
