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
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DEFAULT_MAX_ATTEMPTS, AuditTask, Contract, ContractCommit
from app.db.repository.auth import UserRecord
from app.generated.schemas import ContractStatus, Role, Status
from rag_contracts.enums import ContractType


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


def owner_scope(user: UserRecord | None) -> tuple[UUID | None, bool]:
    """归属过滤的唯一策略：内部调用/ADMIN 可跨归属，普通用户只看自己的合同。"""
    if user is None or user.role is Role.admin:
        return None, True
    return user.id, False


class ContractCreate(_PersistenceDto):
    """创建合同记录所需字段。"""

    id: UUID | None = None
    filename: str
    file_size: int = Field(ge=0)
    file_hash: str
    storage_path: str
    status: ContractStatus = ContractStatus.uploading
    contract_type: ContractType = ContractType.OTHER
    created_by_id: UUID | None = None
    legacy: bool = False


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
    created_by_id: UUID | None = None
    legacy: bool = False
    created_at: datetime
    updated_at: datetime


#: 重试退避基数/上限（秒）：**唯一处**，指数退避 `base * 2^(attempt-1)` 封顶。
RETRY_BACKOFF_BASE_S = 5.0
RETRY_BACKOFF_CAP_S = 300.0


class TaskRecord(_PersistenceDto):
    """任务单条的持久化投影（含 C62 可靠性字段）。"""

    id: UUID
    contract_id: UUID
    task_type: str
    stage: str
    progress: float
    status: Status
    celery_task_id: str | None
    error_message: str | None
    attempt_count: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    last_heartbeat_at: datetime | None = None
    next_retry_at: datetime | None = None
    error_code: str | None = None
    locked_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def can_retry(self) -> bool:
        """是否还能再试一次（**有限重试唯一判据**：禁止无限重试）。"""
        if self.status is Status.done:
            return False
        return self.attempt_count < max(1, self.max_attempts)

    @property
    def retry_delay_seconds(self) -> float:
        """下一次重试前的等待秒数：`base × 2^(已完成尝试数-1)`，封顶 `RETRY_BACKOFF_CAP_S`。

        例：1 次尝试后失败 → 5s；2 次 → 10s；3 次 → 20s（长退避不会无限增长）。
        """
        exponent = max(0, self.attempt_count - 1)
        return min(RETRY_BACKOFF_CAP_S, RETRY_BACKOFF_BASE_S * (2**exponent))


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
        owner_id: UUID | None = None,
        include_all: bool = True,
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
        if owner_id is not None and not include_all:
            filters.append(Contract.created_by_id == owner_id)

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

    async def get(
        self,
        contract_id: UUID,
        *,
        owner_id: UUID | None = None,
        include_all: bool = True,
    ) -> ContractDetail | None:
        """按主键读取合同；普通用户启用归属过滤，越权按不存在处理。"""
        if owner_id is None or include_all:
            record = await self._session.get(Contract, contract_id)
        else:
            record = await self._session.scalar(
                select(Contract).where(
                    Contract.id == contract_id,
                    Contract.created_by_id == owner_id,
                )
            )
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
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> TaskRecord:
        """创建并 flush 任务记录（C62：**派发前**先建行，celery id 随后回填）。"""
        record = AuditTask(
            contract_id=contract_id,
            task_type=task_type,
            stage="queued",
            progress=0.0,
            status=Status.running,
            celery_task_id=celery_task_id,
            attempt_count=0,
            max_attempts=max(1, int(max_attempts)),
        )
        self._session.add(record)
        await self._session.flush()
        return TaskRecord.model_validate(record)

    async def attach_celery_task_id(
        self,
        task_id: UUID,
        celery_task_id: str,
    ) -> TaskRecord:
        """派发成功后回填 Celery id（⑥：派发前先建行 → 进程中断也能对账）。"""
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            raise LookupError(f"任务不存在: {task_id}")
        record.celery_task_id = celery_task_id
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return TaskRecord.model_validate(record)

    async def mark_failed(
        self,
        task_id: UUID,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> TaskRecord:
        """置终态 `FAILED` 并写 `errorCode` / `completedAt`（⑥-3）。"""
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            raise LookupError(f"任务不存在: {task_id}")
        record.status = Status.failed
        record.error_code = error_code
        record.error_message = error_message
        record.completed_at = datetime.now(UTC)
        record.updated_at = record.completed_at
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

    async def list_stale(
        self,
        *,
        stale_before: datetime,
        lock_expired_before: datetime,
        limit: int = 200,
    ) -> list[TaskRecord]:
        """列出「非终态 + 心跳过期 + 锁未被他人持有」的任务（C63 扫描入口）。

        ⚠️ 判定只看**最后心跳/最后更新**，绝不看 `created_at`（§⑨-1）：
        长任务可能创建很久但一直在推进。
        """
        statement = (
            select(AuditTask)
            .where(
                AuditTask.status == Status.running,
                or_(
                    and_(
                        AuditTask.last_heartbeat_at.is_(None),
                        AuditTask.updated_at < stale_before,
                    ),
                    AuditTask.last_heartbeat_at < stale_before,
                ),
                or_(
                    AuditTask.locked_at.is_(None),
                    AuditTask.locked_at < lock_expired_before,
                ),
            )
            .order_by(AuditTask.updated_at.asc(), AuditTask.id.asc())
            .limit(max(1, int(limit)))
        )
        records = (await self._session.scalars(statement)).all()
        return [TaskRecord.model_validate(record) for record in records]

    async def heartbeat(
        self,
        task_id: UUID,
        *,
        at: datetime | None = None,
    ) -> None:
        """回写心跳时间（网关侧对账写入；worker 只往 Redis 打点）。"""
        moment = at or datetime.now(UTC)
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            raise LookupError(f"任务不存在: {task_id}")
        record.last_heartbeat_at = moment
        record.updated_at = moment
        await self._session.flush()

    async def claim(
        self,
        task_id: UUID,
        *,
        now: datetime,
        lock_expired_before: datetime,
    ) -> bool:
        """原子抢锁：只有「仍 RUNNING + 锁空闲/过期」的行会被本次 sweep 拿到。

        Returns:
            True 表示本次调用拿到处理权；False 表示已被其它 sweep 持有（**不得重复派发**）。
        """
        statement = (
            update(AuditTask)
            .where(
                AuditTask.id == task_id,
                AuditTask.status == Status.running,
                or_(
                    AuditTask.locked_at.is_(None),
                    AuditTask.locked_at < lock_expired_before,
                ),
            )
            .values(locked_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(statement)
        return bool(result.rowcount)

    async def record_attempt(
        self,
        task_id: UUID,
        *,
        error_code: str,
        next_retry_at: datetime,
        at: datetime | None = None,
    ) -> TaskRecord:
        """记一次尝试（重试派发后调用）：`attempt_count+1` + errorCode + 下次重试时间。

        同时刷新 `last_heartbeat_at` 并释放 `locked_at` —— 重试窗口从本次重新起算，
        避免同一个 stale 窗口被连续重复派发。
        """
        moment = at or datetime.now(UTC)
        record = await self._session.get(AuditTask, task_id)
        if record is None:
            raise LookupError(f"任务不存在: {task_id}")
        record.attempt_count = int(record.attempt_count or 0) + 1
        record.error_code = error_code
        record.next_retry_at = next_retry_at
        record.last_heartbeat_at = moment
        record.locked_at = None
        record.updated_at = moment
        await self._session.flush()
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
    "RETRY_BACKOFF_BASE_S",
    "RETRY_BACKOFF_CAP_S",
    "CommitItemRecord",
    "CommitRepository",
    "ContractCreate",
    "ContractDetail",
    "ContractRepository",
    "ContractSummary",
    "TaskRecord",
    "TaskRepository",
]
