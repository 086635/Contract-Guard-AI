"""Agent 路由（AI 重写 / Diff）。

对应 data_scheme.md §8.4（RewriteResponse 含 CoT + 溯源）、§10（分层 Memory）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.generated.schemas import AgentRewriteRequest, RewriteResponse
from app.services.mock_factory import MockDataFactory

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/rewrite", response_model=RewriteResponse, status_code=200)
async def rewrite_clause(body: AgentRewriteRequest) -> RewriteResponse:
    """AI 重写（Diff，含 CoT + 溯源）。需 X-Request-ID 幂等。

    TODO：校验 AgentRewriteRequest（生成类型），调用 ai_engine.rewriter，
    经分层 ScopedClauseMemory + RagRuleContext 产出 RewriteResponse。
    """
    return MockDataFactory.rewrite_clause(body)
