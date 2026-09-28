"""Agent 路由（AI 重写 / Diff）。

对应 data_scheme.md §8.4（RewriteResponse 含 CoT + 溯源）、§10（分层 Memory）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_service_context
from app.generated.schemas import AgentRewriteRequest, RewriteResponse
from app.services.agent_service import (
    AgentDispatchError,
    AgentRewriteFailedError,
    AgentRewriteTimeoutError,
    AgentService,
)
from app.services.context import ServiceContext
from app.services.envelope import build_success_response

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/rewrite", response_model=RewriteResponse, status_code=200)
async def rewrite_clause(
    body: AgentRewriteRequest,
    context: ServiceContext = Depends(get_service_context),
) -> RewriteResponse:
    """AI 重写（Diff，含 CoT + 溯源）。需 X-Request-ID 幂等。"""
    try:
        data = await AgentService(context).rewrite(body)
    except AgentRewriteTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except (AgentDispatchError, AgentRewriteFailedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    return build_success_response(RewriteResponse, data)
