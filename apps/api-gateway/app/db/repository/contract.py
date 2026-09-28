"""交付模块 C4：合同与任务仓储。

设计要点：
1. Repository 只依赖 SQLAlchemy session 与 C1 模型，不承载业务编排。
2. 返回表级持久化 DTO，不向路由层泄漏 ORM 实例。
3. OpenAPI 全量 `Contract` 聚合不在本卡实现，避免伪造未持久化字段。

对应：`文件集合/codex/4.C4合同与任务DAL.md`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditTask, Contract, ContractCommit
from app.generated.schemas import ContractStatus, Status
from rag_contracts.enums import ContractType


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class ContractCreate(_PersistenceDto):
    """创建合同记录所需字段。"""

    id: UUID | None = None
    filename: str
    file_size: int = Field(ge=0)
    file_hash: str
    storage_path: str
    status: ContractStatus = ContractStatus.uploading
    contract_type: ContractType = ContractType.OTHER


class ContractSummary(_PersistenceDto):
    """合同列表的持久化投影。"""

    id: UUID
    filename: str
    contract_type: ContractType
    status: ContractStatus
    created_at: datetime


class ContractDetail(_PersistenceDto):
    """合同单条的持久化投影。"""

    id: UUID
    filename: str
    file_size: int
    file_hash: str
    storage_path: str
    status: ContractStatus
    contract_type: ContractType
    version: int
    created_at: datetime
    updated_at: datetime


class TaskRecord(_PersistenceDto):
    """任务单条的持久化投影。"""

    id: UUID
    contract_id: UUID
    task_type: str
    stage: str
    progress: float
    status: Status
    celery_task_id: str | None
    error_message: str | None


class CommitItemRecord(_PersistenceDto):
    """合同提交批次明细投影。"""

    id: UUID
    contract_id: UUID
    request_id: str
    item_id: str
    accepted: bool
    version: int
    created_at: datetime


class ContractRepository:
    """合同表仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(
        self,
        page: int,
        page_size: int,
        *,
        contract_type: ContractType | None = None,
        status: ContractStatus | None = None,
    ) -> tuple[list[ContractSummary], int]:
        """分页返回合同摘要与总数。"""
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1..100 之间")

        filters = []
        if contract_type is not None:
            filters.append(Contract.contract_type == contract_type)
        if status is not None:
            filters.append(Contract.status == status)

        count_statement = select(func.count()).select_from(Contract)
        if filters:
            count_statement = count_statement.where(*filters)
        total = await self._session.scalar(count_statement)
        statement = (
            select(Contract)
            .order_by(Contract.created_at.desc(), Contract.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if filters:
            statement = statement.where(*filters)
        records = (await self._session.scalars(statement)).all()
        return [ContractSummary.model_validate(record) for record in records], int(
            total or 0
        )

    async def get(self, contract_id: UUID) -> ContractDetail | None:
        """按主键读取合同。"""
        record = await self._session.get(Contract, contract_id)
        if record is None:
            return None
        return ContractDetail.model_validate(record)

    async def create(self, meta: ContractCreate) -> ContractDetail:
        """创建并 flush 合同记录。"""
        payload = meta.model_dump(exclude_none=True)
        payload.setdefault("id", uuid4())
        record = Contract(**payload)
        self._session.add(record)
        await self._session.flush()
        return ContractDetail.model_validate(record)

    async def update_status(
        self,
        contract_id: UUID,
        status: ContractStatus,
    ) -> None:
        """更新合同状态并刷新 `updated_at`。"""
        record = await self._session.get(Contract, contract_id)
        if record is None:
            raise LookupError(f"合同不存在: {contract_id}")
        record.status = status
        record.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def increment_version(self, contract_id: UUID) -> int:
        """增加合同版本并返回新版本。"""
        record = await self._session.get(Contract, contract_id)
        if record is None:
            raise LookupError(f"合同不存在: {contract_id}")
        record.version += 1
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return record.version


class CommitRepository:
    """合同提交明细仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_request(self, request_id: str) -> list[CommitItemRecord]:
        """按幂等请求 ID 读取提交明细。"""
        statement = (
            select(ContractCommit)
            .where(ContractCommit.request_id == request_id)
            .order_by(ContractCommit.item_id.asc())
        )
        records = (await self._session.scalars(statement)).all()
        return [CommitItemRecord.model_validate(record) for record in records]

    async def create_items(
        self,
        contract_id: UUID,
        request_id: str,
        version: int,
        items: list[tuple[str, bool]],
    ) -> None:
        """批量写入提交明细，重复请求项幂等跳过。"""
        if not items:
            return
        rows = [
            {
                "contract_id": contract_id,
                "request_id": request_id,
                "item_id": item_id,
                "accepted": accepted,
                "version": version,
            }
            for item_id, accepted in items
        ]
        statement = (
            pg_insert(ContractCommit)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["request_id", "item_id"],
            )
        )
        await self._session.execute(statement)


class TaskRepository:
    """任务表仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        contract_id: UUID,
        task_type: str,
        celery_task_id: str | None,
    ) -> TaskRecord:
        """创建并 flush 任务记录。"""
        record = AuditTask(
            contract_id=contract_id,
            task_type=task_type,
            stage="queued",
            progress=0.0,
            status=Status.running,
            celery_task_id=celery_task_id,
        )
        self._session.add(record)
        await self._session.flush()
        return TaskRecord.model_validate(record)

    async def get(self, task_id: UUID) -> TaskRecord | None:
        """按主键读取任务。"""
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            return None
        return TaskRecord.model_validate(record)

    async def get_latest_by_contract(
        self,
        contract_id: UUID,
        task_type: str,
    ) -> TaskRecord | None:
        """读取合同下指定类型的最新任务。"""
        statement = (
            select(AuditTask)
            .where(
                AuditTask.contract_id == contract_id,
                AuditTask.task_type == task_type,
            )
            .order_by(AuditTask.created_at.desc(), AuditTask.id.desc())
            .limit(1)
        )
        record = await self._session.scalar(statement)
        if record is None:
            return None
        return TaskRecord.model_validate(record)

    async def update_status(
        self,
        task_id: UUID,
        status: Status,
        error_message: str | None = None,
    ) -> None:
        """更新任务状态与错误信息。"""
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            raise LookupError(f"任务不存在: {task_id}")
        record.status = status
        record.error_message = error_message
        record.updated_at = datetime.now(UTC)
        await self._session.flush()

    async def update_progress(
        self,
        task_id: UUID,
        stage: str,
        progress: float,
    ) -> None:
        """更新任务阶段与 `0..1` 进度。"""
        if not 0.0 <= progress <= 1.0:
            raise ValueError("progress 必须在 0..1 之间")
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            raise LookupError(f"任务不存在: {task_id}")
        record.stage = stage
        record.progress = progress
        record.updated_at = datetime.now(UTC)
        await self._session.flush()


__all__ = [
    "CommitItemRecord",
    "CommitRepository",
    "ContractCreate",
    "ContractDetail",
    "ContractRepository",
    "ContractSummary",
    "TaskRecord",
    "TaskRepository",
]
