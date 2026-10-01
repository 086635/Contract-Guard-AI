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

from app.config import settings
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
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
from app.services.projector import ContractProjector
from app.services.storage import UploadObjectError, validate_upload_object
from rag_contracts.enums import ContractType

#: 单文件大小上限（唯一处；C56 §⑥ 沿用既有 50 MB 约定）。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: 声明文件类型 → 允许的扩展名（**服务端准入唯一处**，C56 §⑥）。
#: 扩展名只用于「声明的类型与文件名是否自洽」；真实内容判据是 C57 的 magic sniff。
_ALLOWED_EXTENSIONS: dict[FileType, frozenset[str]] = {
    FileType.pdf_native: frozenset({".pdf"}),
    FileType.pdf_scanned: frozenset({".pdf"}),
    FileType.docx: frozenset({".docx"}),
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

    async def upload(self, body: UploadBody) -> TaskProgress:
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

        contract = await self._contracts.create(
            ContractCreate(
                filename=body.file_name,
                file_size=max(0, object_info.size),
                file_hash=file_hash,
                storage_path=body.file_key,
                status=ContractStatus.parsing,
            )
        )
        celery_task_id = self._context.task_dispatcher.send(
            TaskKind.PARSE_DOC,
            args=(body.file_key, str(contract.id)),
        )
        task = await self._tasks.create(
            contract.id,
            TaskKind.PARSE_DOC.name,
            celery_task_id,
        )
        await self._context.session.commit()
        return TaskProgress(
            taskId=str(task.id),
            stage=task.stage,
            percent=task.progress,
            status=task.status,
            result={"contractId": str(contract.id)},
        )

    async def list(
        self,
        page: int,
        page_size: int,
        *,
        contract_type: str | None = None,
        status: str | None = None,
    ) -> tuple[list[s.ContractSummary], int]:
        """分页聚合合同摘要。"""
        parsed_type = ContractType(contract_type) if contract_type is not None else None
        parsed_status = s.ContractStatus(status) if status is not None else None
        details, total = await self._contracts.list(
            page,
            page_size,
            contract_type=parsed_type,
            status=parsed_status,
        )
        summaries = []
        for detail in details:
            risks = await self._risks.list_by_contract(detail.id)
            conflicts = await self._risks.list_conflicts_by_contract(detail.id)
            summaries.append(ContractProjector.summary(detail, risks, conflicts))
        return summaries, total

    async def get(self, contract_id: UUID) -> s.Contract | None:
        """聚合单合同完整详情。"""
        detail = await self._contracts.get(contract_id)
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
