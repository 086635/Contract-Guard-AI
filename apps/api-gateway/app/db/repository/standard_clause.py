"""C70 标准条款库仓储（人工维护源：PostgreSQL 唯一落点）。

设计要点：
1. 本表是标准条款的**唯一人工维护源**；RAG 向量集合是它的**派生检索索引**（可重建、可幂等重放）。
2. 删除语义 = 软停用（`is_enabled=False`），禁止物理删除造成检索悬挂（C70 §⑨-2）。
3. 乐观锁：`update` 必须带当前 `version`，不一致抛 `StandardClauseVersionConflictError`。

对应：`文件集合/codex/4.C70标准条款库数据与服务.md`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StandardClause as StandardClauseModel
from app.generated.schemas import ContractType
from rag_contracts.enums import ContractType as RagContractType


class StandardClauseNotFoundError(LookupError):
    """标准条款不存在。"""


class StandardClauseVersionConflictError(RuntimeError):
    """乐观锁冲突：客户端 version 与库内不一致。"""


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class StandardClauseCreate(_PersistenceDto):
    """新建标准条款所需字段。"""

    title: str = Field(min_length=1, max_length=200)
    contract_type: ContractType
    text: str = Field(min_length=1, max_length=8000)
    source: str = Field(default="manual", max_length=200)


class StandardClausePatch(_PersistenceDto):
    """局部更新字段；`None` 表示不修改。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    contract_type: ContractType | None = None
    text: str | None = Field(default=None, min_length=1, max_length=8000)
    is_enabled: bool | None = None
    version: int = Field(ge=1)


class StandardClauseRecord(_PersistenceDto):
    """标准条款安全投影。"""

    id: UUID
    title: str
    contract_type: ContractType
    text: str
    is_enabled: bool
    version: int
    source: str
    created_at: datetime
    updated_at: datetime


def _ensure_utc(value: datetime) -> datetime:
    """把回读时间归一为 UTC（SQLite 等无时区后端回读为 naive）。"""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_record(record: StandardClauseModel) -> StandardClauseRecord:
    """把 ORM 投影为不暴露 ORM 实例的 DTO。"""
    return StandardClauseRecord(
        id=record.id,
        title=record.title,
        contract_type=ContractType(record.contract_type.value),
        text=record.text,
        is_enabled=record.is_enabled,
        version=record.version,
        source=record.source,
        created_at=_ensure_utc(record.created_at),
        updated_at=_ensure_utc(record.updated_at),
    )


class StandardClauseRepository:
    """标准条款 CRUD / 分页仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: StandardClauseCreate) -> StandardClauseRecord:
        """新建标准条款并 flush；调用方负责事务提交与索引同步。"""
        now = datetime.now(UTC)
        record = StandardClauseModel(
            id=uuid4(),
            title=payload.title.strip(),
            contract_type=RagContractType(payload.contract_type.value),
            text=payload.text.strip(),
            is_enabled=True,
            version=1,
            source=payload.source.strip() or "manual",
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        return _to_record(record)

    async def get(self, clause_id: UUID) -> StandardClauseRecord | None:
        """按 ID 读取单条标准条款。"""
        record = await self._session.get(StandardClauseModel, clause_id)
        return _to_record(record) if record is not None else None

    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        query: str | None = None,
        contract_type: ContractType | None = None,
        is_enabled: bool | None = None,
    ) -> tuple[list[StandardClauseRecord], int]:
        """按标题/正文关键字、合同类型、启停状态稳定分页。"""
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1..100 之间")

        filters = []
        if query:
            keyword = f"%{query.strip()}%"
            filters.append(
                or_(
                    StandardClauseModel.title.ilike(keyword),
                    StandardClauseModel.text.ilike(keyword),
                )
            )
        if contract_type is not None:
            filters.append(
                StandardClauseModel.contract_type
                == RagContractType(contract_type.value)
            )
        if is_enabled is not None:
            filters.append(StandardClauseModel.is_enabled.is_(is_enabled))

        count_statement = select(func.count()).select_from(StandardClauseModel)
        statement = (
            select(StandardClauseModel)
            .order_by(
                StandardClauseModel.updated_at.desc(), StandardClauseModel.id.asc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if filters:
            count_statement = count_statement.where(*filters)
            statement = statement.where(*filters)

        total = await self._session.scalar(count_statement)
        records = (await self._session.scalars(statement)).all()
        return [_to_record(record) for record in records], int(total or 0)

    async def list_all(self) -> list[StandardClauseRecord]:
        """读取全量条款（索引全量重建用；含停用条目）。"""
        statement = select(StandardClauseModel).order_by(
            StandardClauseModel.created_at.asc(),
            StandardClauseModel.id.asc(),
        )
        records = (await self._session.scalars(statement)).all()
        return [_to_record(record) for record in records]

    async def update(
        self, clause_id: UUID, patch: StandardClausePatch
    ) -> StandardClauseRecord:
        """带乐观锁的局部更新；命中即 version+1。"""
        record = await self._session.get(StandardClauseModel, clause_id)
        if record is None:
            raise StandardClauseNotFoundError(str(clause_id))
        if record.version != patch.version:
            raise StandardClauseVersionConflictError(
                f"version 冲突：库内 {record.version}，请求 {patch.version}"
            )

        if patch.title is not None:
            record.title = patch.title.strip()
        if patch.contract_type is not None:
            record.contract_type = RagContractType(patch.contract_type.value)
        if patch.text is not None:
            record.text = patch.text.strip()
        if patch.is_enabled is not None:
            record.is_enabled = patch.is_enabled

        record.version += 1
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return _to_record(record)


__all__ = [
    "StandardClauseCreate",
    "StandardClauseNotFoundError",
    "StandardClausePatch",
    "StandardClauseRecord",
    "StandardClauseRepository",
    "StandardClauseVersionConflictError",
]
