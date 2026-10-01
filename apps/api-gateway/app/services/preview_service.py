"""C58 预览服务：状态查询 / 短期 URL 签发 / 重试派发。

设计要点：
1. `previewUrl` **运行时签发**（`MinioObjectStorage.presign_get`），永不落库；
   库里只有内部对象 key，且绝不进 API 响应。
2. 显式状态机：PDF 上传即 `DONE`（原文即可预览）；DOCX 初始 `PENDING`，
   由 `convert_doc`（C59）转换后置 `DONE`；失败置 `FAILED` 且可重试。
3. 重试**幂等**：`contract_previews.convert_task_id` 记录在飞转换任务，
   `RUNNING` 且已有任务时不再重复派发。
4. 权限与存在性：合同不可见/不存在都由路由统一映射 404，不泄露存在性。

对应：`文件集合/codex/4.C58预览元数据与预览API.md`。
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.db.repository.contract import ContractDetail, ContractRepository
from app.db.repository.preview import PreviewRecord, PreviewRepository
from app.generated.schemas import PreviewData, PreviewStatus, Status, TaskProgress
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind

#: 预览 URL 有效期（秒）：短期签名，前端到期后重新 GET。
PREVIEW_URL_TTL_SECONDS = 900

_PDF_SUFFIXES = frozenset({".pdf"})
_DOCX_SUFFIXES = frozenset({".docx"})

#: 预览来源类型（唯一处）：`PDF` 直接用原文，`DOCX` 需经 `convert` 队列。
SOURCE_PDF = "PDF"
SOURCE_DOCX = "DOCX"


def _failure_reason(result: dict) -> str:
    """从转换结果里取第一条安全摘要（没有则给通用码）。"""
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        return str(warnings[0])
    return "CONVERT_FAILED"


def source_kind_of(filename: str) -> str | None:
    """按存储文件名判定预览来源类型。

    注意：这是**选择预览管线**用的分类，不是上传安全判据——
    真实内容类型已由 C56/C57 在服务端校验（magic sniff + 结构校验）。
    """
    suffix = Path(filename).suffix.lower()
    if suffix in _PDF_SUFFIXES:
        return SOURCE_PDF
    if suffix in _DOCX_SUFFIXES:
        return SOURCE_DOCX
    return None


def initial_preview_status(filename: str) -> PreviewStatus:
    """返回该来源类型的**初始**预览状态（PDF 立即 DONE，DOCX 待转换）。"""
    kind = source_kind_of(filename)
    if kind == SOURCE_PDF:
        return PreviewStatus.done
    if kind == SOURCE_DOCX:
        return PreviewStatus.pending
    return PreviewStatus.failed


def preview_object_key_for(contract: ContractDetail) -> str | None:
    """PDF 的预览对象就是原文对象；其它来源等待 C59 写入派生对象。"""
    if source_kind_of(contract.filename) == SOURCE_PDF:
        return contract.storage_path
    return None


class PreviewService:
    """合同预览元数据服务。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        previews: PreviewRepository | None = None,
        contracts: ContractRepository | None = None,
    ) -> None:
        """构造服务。

        `previews` / `contracts` 可注入替身（与 `AuthService` 的可注入协作者同款），
        使状态机与幂等分支**离线可测**；生产路径默认用当前 session 的真实仓储。
        """
        self._context = context
        self._contracts = contracts or ContractRepository(context.session)
        self._previews = previews or PreviewRepository(context.session)

    async def get(self, contract_id: UUID) -> PreviewData:
        """读取预览元数据；缺失时按来源类型初始化一行（幂等）。"""
        contract = await self._require_contract(contract_id)
        record = await self._previews.get_by_contract(contract_id)
        if record is None:
            record = await self._initialize(contract)
            await self._context.session.commit()
        elif record.status is PreviewStatus.running:
            reconciled = await self.reconcile_convert(contract_id)
            if reconciled is not None:
                record = reconciled
            await self._context.session.commit()
        return self._to_preview_data(record)

    async def retry(self, contract_id: UUID) -> TaskProgress:
        """重试预览转换派发（幂等：在飞任务不重复派发）。"""
        contract = await self._require_contract(contract_id)
        record = await self._previews.get_by_contract(contract_id)
        if record is None:
            record = await self._initialize(contract)

        if record.status is PreviewStatus.done:
            await self._context.session.commit()
            return self._to_progress(record, Status.done, stage="done", percent=1.0)
        if record.status is PreviewStatus.running and record.convert_task_id:
            await self._context.session.commit()
            return self._to_progress(
                record, Status.running, stage="converting", percent=0.0
            )

        # 参数顺序即 C59 §⑤ 的跨进程契约：`convert_doc(contract_id, source_key)`。
        celery_task_id = self._context.task_dispatcher.send(
            TaskKind.CONVERT_DOC,
            args=(str(contract_id), contract.storage_path),
        )
        record = await self._previews.mark_running(record.id, celery_task_id)
        await self._context.session.commit()
        return self._to_progress(
            record, Status.running, stage="converting", percent=0.0
        )

    async def reconcile_convert(self, contract_id: UUID) -> PreviewRecord | None:
        """按 Celery 转换结果回写预览状态（**网关**职责；worker 不写业务 DB）。

        - 仍 `RUNNING` → 原样返回（下一次读取或 C63 reconciler 再试）；
        - Celery `DONE` 且结果 `status=SUCCESS` → 写派生 key/版本，置 `DONE`；
        - Celery `FAILED` 或结果 `status=FAILED` → 置 `FAILED`（附安全摘要）。

        Returns:
            回写后的记录；没有预览行/没有在飞任务/状态非 `RUNNING` 时返回当前值。
        """
        record = await self._previews.get_by_contract(contract_id)
        if record is None or not record.convert_task_id:
            return record
        if record.status is not PreviewStatus.running:
            return record

        snapshot = self._context.task_dispatcher.get_status(record.convert_task_id)
        if snapshot is None:
            return record
        result = snapshot.result if isinstance(snapshot.result, dict) else {}
        if snapshot.status == "FAILED" or result.get("status") == "FAILED":
            return await self._previews.mark_failed(
                record.id,
                error_message=_failure_reason(result),
            )
        if snapshot.status == "DONE" and result.get("status") == "SUCCESS":
            object_key = result.get("storagePath")
            if not isinstance(object_key, str) or not object_key:
                return await self._previews.mark_failed(
                    record.id,
                    error_message="CONVERT_NO_OUTPUT_KEY",
                )
            version = result.get("converterVersion")
            return await self._previews.mark_done(
                record.id,
                preview_object_key=object_key,
                converter_version=str(version) if version else None,
            )
        return record

    async def _require_contract(self, contract_id: UUID) -> ContractDetail:
        """读取合同；不存在抛 `ContractNotFoundError`（路由统一映射 404）。"""
        contract = await self._contracts.get(contract_id)
        if contract is None:
            raise ContractNotFoundError(str(contract_id))
        return contract

    async def _initialize(self, contract: ContractDetail) -> PreviewRecord:
        """按来源类型建立初始预览行（PDF 立即 DONE）。"""
        status = initial_preview_status(contract.filename)
        return await self._previews.create(
            contract.id,
            status=status,
            preview_object_key=preview_object_key_for(contract),
            error_message=(
                "ERR_UNSUPPORTED_PREVIEW_SOURCE"
                if status is PreviewStatus.failed
                else None
            ),
        )

    def _to_preview_data(self, record: PreviewRecord) -> PreviewData:
        """投影为对外 DTO：**只**输出短期签名 URL，绝不输出对象 key。"""
        preview_url: str | None = None
        expires_in = 0
        if record.status is PreviewStatus.done and record.preview_object_key:
            preview_url = self._context.object_storage.presign_get(
                record.preview_object_key,
                PREVIEW_URL_TTL_SECONDS,
            )
            expires_in = PREVIEW_URL_TTL_SECONDS
        return PreviewData(
            contractId=str(record.contract_id),
            status=record.status,
            previewUrl=preview_url,
            expiresIn=expires_in,
            pageCount=record.page_count,
            converterVersion=record.converter_version,
            createdAt=record.created_at,
        )

    @staticmethod
    def _to_progress(
        record: PreviewRecord,
        status: Status,
        *,
        stage: str,
        percent: float,
    ) -> TaskProgress:
        """把预览行投影为任务进度（`taskId` 为在飞 Celery id）。"""
        return TaskProgress(
            taskId=record.convert_task_id or str(record.id),
            stage=stage,
            percent=percent,
            status=status,
            result={
                "contractId": str(record.contract_id),
                "previewStatus": record.status.value,
            },
        )


__all__ = [
    "PREVIEW_URL_TTL_SECONDS",
    "SOURCE_DOCX",
    "SOURCE_PDF",
    "PreviewService",
    "initial_preview_status",
    "preview_object_key_for",
    "source_kind_of",
]
