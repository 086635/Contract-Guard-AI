"""Agent 路由（AI 重写 / Diff）。

对应 data_scheme.md §8.4（RewriteResponse 含 CoT + 溯源）、§10（分层 Memory）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import (
    get_audit_service,
    get_current_user,
    get_service_context,
    write_audit_event,
)
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord
from app.generated.schemas import AgentRewriteRequest, RewriteResponse
from app.services.agent_service import (
    AgentDispatchError,
    AgentRewriteFailedError,
    AgentRewriteTimeoutError,
    AgentService,
)
from app.services.audit_service import AuditService
from app.services.context import ServiceContext
from app.services.envelope import build_success_response

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/rewrite", response_model=RewriteResponse, status_code=200)
async def rewrite_clause(
    body: AgentRewriteRequest,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: ServiceContext = Depends(get_service_context),
) -> RewriteResponse:
    """AI 重写（Diff，含 CoT + 溯源）。需 X-Request-ID 幂等。"""
    try:
        data = await AgentService(context).rewrite(body)
    except AgentRewriteTimeoutError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.rewrite,
            "CONTRACT",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=body.contract_id,
            metadata={"clauseId": body.clause_id, "errorCode": "TIMEOUT"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except (AgentDispatchError, AgentRewriteFailedError) as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.rewrite,
            "CONTRACT",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=body.contract_id,
            metadata={"clauseId": body.clause_id, "errorCode": type(exc).__name__},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    await write_audit_event(
        audit,
        request,
        AuditAction.rewrite,
        "CONTRACT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=body.contract_id,
        metadata={"clauseId": body.clause_id, "xRequestId": body.x_request_id},
    )
    await context.session.commit()
    return build_success_response(RewriteResponse, data)