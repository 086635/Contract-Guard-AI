"""C50 审计事件查询路由（ADMIN-only）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_audit_service, require_roles
from app.db.models import AuditAction as AuditActionModel
from app.db.models import AuditOutcome as AuditOutcomeModel
from app.db.repository.audit import AuditEventRecord
from app.generated.schemas import (
    AuditAction,
    AuditEvent,
    AuditEventListData,
    AuditEventListResponse,
    AuditOutcome,
    Role,
)
from app.services.audit_service import AuditService
from app.services.envelope import build_success_response

router = APIRouter(prefix="/audit", tags=["Audit"])


def _to_dto(record: AuditEventRecord) -> AuditEvent:
    """把仓储记录投影为 OpenAPI AuditEvent。"""
    return AuditEvent(
        id=str(record.id),
        actorId=str(record.actor_id) if record.actor_id else None,
        actorRole=record.actor_role,
        action=record.action.value,
        resourceType=record.resource_type,
        resourceId=record.resource_id,
        outcome=record.outcome.value,
        ipAddress=record.ip_address,
        userAgent=record.user_agent,
        traceId=record.trace_id,
        metadata=record.metadata,
        createdAt=record.created_at,
    )


@router.get(
    "/events",
    response_model=AuditEventListResponse,
    status_code=200,
)
async def list_audit_events(
    _user: Annotated[object, Depends(require_roles(Role.admin))],
    service: Annotated[AuditService, Depends(get_audit_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    actor_id: Annotated[UUID | None, Query(alias="actorId")] = None,
    action: Annotated[AuditAction | None, Query()] = None,
    resource_type: Annotated[str | None, Query(alias="resourceType")] = None,
    resource_id: Annotated[str | None, Query(alias="resourceId")] = None,
    outcome: Annotated[AuditOutcome | None, Query()] = None,
    trace_id: Annotated[str | None, Query(alias="traceId")] = None,
    created_from: Annotated[datetime | None, Query(alias="from")] = None,
    created_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> AuditEventListResponse:
    """ADMIN 按过滤条件稳定分页查询审计事件。"""
    records, total = await service.list(
        page,
        page_size,
        actor_id=actor_id,
        action=AuditActionModel(action) if action is not None else None,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=AuditOutcomeModel(outcome) if outcome is not None else None,
        trace_id=trace_id,
        created_from=created_from,
        created_to=created_to,
    )
    data = AuditEventListData(
        items=[_to_dto(record) for record in records],
        total=total,
        page=page,
        pageSize=page_size,
        hasMore=page * page_size < total,
    )
    return build_success_response(AuditEventListResponse, data)


__all__ = ["router"]