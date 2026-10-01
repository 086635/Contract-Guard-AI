"""C58 预览元数据仓储。

一合同一行（`uq_contract_previews_contract`）；`preview_object_key` 是内部 MinIO key，
只用于运行时签名，不参与任何对外响应投影。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ContractPreview
from app.generated.schemas import PreviewStatus


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class PreviewRecord(_PersistenceDto):
    """预览元数据投影（含内部对象 key，仅在服务层内部使用）。"""

    id: UUID
    contract_id: UUID
    status: PreviewStatus
    preview_object_key: str | None
    convert_task_id: str | None
    page_count: int | None
    converter_version: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class PreviewRepository:
    """预览元数据仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_contract(self, contract_id: UUID) -> PreviewRecord | None:
        """读取合同对应的预览行（一合同一行）。"""
        statement = (
            select(ContractPreview)
            .where(ContractPreview.contract_id == contract_id)
            .limit(1)
        )
        record = await self._session.scalar(statement)
        if record is None:
            return None
        return PreviewRecord.model_validate(record)

    async def create(
        self,
        contract_id: UUID,
        *,
        status: PreviewStatus,
        preview_object_key: str | None = None,
        error_message: str | None = None,
    ) -> PreviewRecord:
        """创建并 flush 预览行。"""
        record = ContractPreview(
            id=uuid4(),
            contract_id=contract_id,
            status=PreviewStatus(status),
            preview_object_key=preview_object_key,
            error_message=error_message,
        )
        self._session.add(record)
        await self._session.flush()
        return PreviewRecord.model_validate(record)

    async def mark_running(
        self,
        preview_id: UUID,
        convert_task_id: str,
    ) -> PreviewRecord:
        """把预览置为 `RUNNING` 并登记在飞转换任务（重试幂等依据）。"""
        record = await self._session.get(ContractPreview, preview_id)
        if record is None:
            raise LookupError(f"预览记录不存在: {preview_id}")
        record.status = PreviewStatus.running
        record.convert_task_id = convert_task_id
        await self._session.flush()
        return PreviewRecord.model_validate(record)

    async def mark_done(
        self,
        preview_id: UUID,
        *,
        preview_object_key: str,
        converter_version: str | None,
        page_count: int | None = None,
    ) -> PreviewRecord:
        """转换成功：写入派生对象 key / 转换器版本，置 `DONE`。"""
        record = await self._session.get(ContractPreview, preview_id)
        if record is None:
            raise LookupError(f"预览记录不存在: {preview_id}")
        record.status = PreviewStatus.done
        record.preview_object_key = preview_object_key
        record.converter_version = converter_version
        if page_count is not None:
            record.page_count = page_count
        record.error_message = None
        await self._session.flush()
        return PreviewRecord.model_validate(record)

    async def mark_failed(
        self,
        preview_id: UUID,
        *,
        error_message: str,
    ) -> PreviewRecord:
        """转换失败：置 `FAILED` 并记录安全摘要（原文不动，可重试）。"""
        record = await self._session.get(ContractPreview, preview_id)
        if record is None:
            raise LookupError(f"预览记录不存在: {preview_id}")
        record.status = PreviewStatus.failed
        record.error_message = error_message
        await self._session.flush()
        return PreviewRecord.model_validate(record)


__all__ = [
    "PreviewRecord",
    "PreviewRepository",
]
