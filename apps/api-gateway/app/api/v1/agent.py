"""Agent 路由（AI 重写 / Diff）。

对应 data_scheme.md §8.4（RewriteResponse 含 CoT + 溯源）、§10（分层 Memory）。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/agent", tags=["Agent"])


@router.post("/rewrite")
async def rewrite_clause(body: dict) -> dict:
    """AI 重写（Diff，含 CoT + 溯源）。需 X-Request-ID 幂等。

    TODO：校验 AgentRewriteRequest（生成类型），调用 ai_engine.rewriter，
    经分层 ScopedClauseMemory + RagRuleContext 产出 RewriteResponse。
    """
    raise HTTPException(status_code=501, detail="rewrite_clause 待实现")
