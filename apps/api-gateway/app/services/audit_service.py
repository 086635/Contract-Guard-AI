"""C50 审计采集与查询服务。

所有业务动作都必须经 `AuditService.write_event()` 写审计；本模块集中完成
metadata 白名单、敏感字段脱敏、trace/IP/UA 归一化，并在写失败时告警但不阻断主业务。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditAction, AuditOutcome
from app.db.repository.audit import (
    AuditEventCreate,
    AuditEventRecord,
    AuditRepository,
)
from app.generated.schemas import Role
from cg_observability import get_trace_id

logger = logging.getLogger("contract_guard.audit")

_ALLOWED_METADATA_KEYS = frozenset(
    {
        "contractId",
        "contractType",
        "clauseId",
        "taskId",
        "xRequestId",
        "reportId",
        "userId",
        "role",
        "targetType",
        "targetId",
        "verdict",
        "fileName",
        "fileType",
        "sizeBytes",
        "status",
        "stage",
        "version",
        "count",
        "method",
        "path",
        "reason",
        "errorCode",
        "exportFormat",
        "downloadType",
        "password",
        "token",
        "accessToken",
        "refreshToken",
        "authorization",
        "cookie",
        "secret",
        "apiKey",
        "prompt",
        "promptTemplate",
        "originalText",
        "content",
        "body",
        "requestBody",
    }
)

_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "password",
        "token",
        "accessToken",
        "refreshToken",
        "authorization",
        "cookie",
        "secret",
        "apiKey",
        "prompt",
        "promptTemplate",
        "originalText",
        "content",
        "body",
        "requestBody",
    }
)

_MAX_STRING_LENGTH = 256
_MAX_LIST_LENGTH = 20


def sanitize_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """只保留白名单 key，并脱敏高风险字段。"""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ALLOWED_METADATA_KEYS:
            continue
        if key in _SENSITIVE_METADATA_KEYS:
            result[key] = "[REDACTED]"
            continue
        normalized = _normalize_value(value)
        if normalized is not None:
            result[key] = normalized
    return result


def _normalize_value(value: Any) -> Any:
    """把 metadata 值限制为简单、可 JSON 序列化的摘要。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STRING_LENGTH]
    if isinstance(value, list):
        items = [_normalize_value(item) for item in value[:_MAX_LIST_LENGTH]]
        return [item for item in items if item is not None]
    return None


class AuditService:
    """审计事件唯一采集入口和查询服务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = AuditRepository(session)

    async def write_event(
        self,
        action: AuditAction,
        resource_type: str,
        outcome: AuditOutcome,
        *,
        actor_id: UUID | None = None,
        actor_role: Role | None = None,
        resource_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        """写一条审计事件；失败只告警，不向主业务抛异常。"""
        try:
            async with self._session.begin_nested():
                await self._repository.append(
                    AuditEventCreate(
                        actor_id=actor_id,
                        actor_role=actor_role,
                        action=AuditAction(action),
                        resource_type=resource_type,
                        resource_id=resource_id,
                        outcome=AuditOutcome(outcome),
                        ip_address=ip_address,
                        user_agent=user_agent,
                        trace_id=trace_id or (get_trace_id() or None),
                        metadata=sanitize_metadata(metadata),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 审计失败绝不阻断业务
            logger.warning(
                "audit.write.failed action=%s resource_type=%s: %s",
                action,
                resource_type,
                type(exc).__name__,
            )

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
        """分页查询审计事件。"""
        return await self._repository.list(
            page,
            page_size,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            trace_id=trace_id,
            created_from=created_from,
            created_to=created_to,
        )


__all__ = ["AuditService", "sanitize_metadata"]