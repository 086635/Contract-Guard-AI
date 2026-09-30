"""C49 append-only 审计事件仓储。

审计事件只允许追加和查询；本模块刻意不提供 update/delete 公共方法。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditAction, AuditEvent, AuditOutcome
from app.generated.schemas import Role


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class AuditEventCreate(_PersistenceDto):
    """追加审计事件所需字段。"""

    id: UUID | None = None
    actor_id: UUID | None = None
    actor_role: Role | None = None
    action: AuditAction
    resource_type: str = Field(min_length=1, max_length=64)
    resource_id: str | None = Field(default=None, max_length=128)
    outcome: AuditOutcome
    ip_address: str | None = Field(default=None, max_length=64)
    user_agent: str | None = Field(default=None, max_length=512)
    trace_id: str | None = Field(default=None, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class AuditEventRecord(_PersistenceDto):
    """审计事件安全投影。"""

    id: UUID
    actor_id: UUID | None
    actor_role: Role | None
    action: AuditAction
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    ip_address: str | None
    user_agent: str | None
    trace_id: str | None
    metadata: dict[str, Any]
    created_at: datetime


def _as_utc(value: datetime) -> datetime:
    """把时间转换为 UTC，拒绝 naive datetime。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


def _to_record(record: AuditEvent) -> AuditEventRecord:
    """把 ORM 投影为不暴露 ORM 实例的 DTO。"""
    return AuditEventRecord(
        id=record.id,
        actor_id=record.actor_id,
        actor_role=record.actor_role,
        action=record.action,
        resource_type=record.resource_type,
        resource_id=record.resource_id,
        outcome=record.outcome,
        ip_address=record.ip_address,
        user_agent=record.user_agent,
        trace_id=record.trace_id,
        metadata=record.event_metadata,
        created_at=record.created_at,
    )


class AuditRepository:
    """审计事件 append-only 仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: AuditEventCreate) -> AuditEventRecord:
        """追加一条审计事件并 flush；调用方负责事务提交。"""
        if not isinstance(event.metadata, dict):
            raise ValueError("metadata 必须是 JSON object")
        created_at = _as_utc(event.created_at or datetime.now(UTC))
        record = AuditEvent(
            id=event.id or uuid4(),
            actor_id=event.actor_id,
            actor_role=Role(event.actor_role) if event.actor_role else None,
            action=AuditAction(event.action),
            resource_type=event.resource_type.strip(),
            resource_id=event.resource_id,
            outcome=AuditOutcome(event.outcome),
            ip_address=event.ip_address,
            user_agent=event.user_agent,
            trace_id=event.trace_id,
            event_metadata=event.metadata,
            created_at=created_at,
        )
        self._session.add(record)
        await self._session.flush()
        return _to_record(record)

    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        actor_id: UUID | None = None,
        action: AuditAction | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        outcome: AuditOutcome | None = None,
        trace_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> tuple[list[AuditEventRecord], int]:
        """按过滤条件稳定分页查询审计事件。"""
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1..100 之间")

        filters = []
        if actor_id is not None:
            filters.append(AuditEvent.actor_id == actor_id)
        if action is not None:
            filters.append(AuditEvent.action == AuditAction(action))
        if resource_type is not None:
            filters.append(AuditEvent.resource_type == resource_type)
        if resource_id is not None:
            filters.append(AuditEvent.resource_id == resource_id)
        if outcome is not None:
            filters.append(AuditEvent.outcome == AuditOutcome(outcome))
        if trace_id is not None:
            filters.append(AuditEvent.trace_id == trace_id)
        if created_from is not None:
            filters.append(AuditEvent.created_at >= _as_utc(created_from))
        if created_to is not None:
            filters.append(AuditEvent.created_at <= _as_utc(created_to))

        count_statement = select(func.count()).select_from(AuditEvent)
        statement = (
            select(AuditEvent)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if filters:
            count_statement = count_statement.where(*filters)
            statement = statement.where(*filters)

        total = await self._session.scalar(count_statement)
        records = (await self._session.scalars(statement)).all()
        return [_to_record(record) for record in records], int(total or 0)


__all__ = [
    "AuditEventCreate",
    "AuditEventRecord",
    "AuditRepository",
]