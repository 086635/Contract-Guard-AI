"""条款重写 Agent（rewriter）。

职责：依据自然语言指令改写条款，产出 RewriteResponse（§8.4），包含
before/after、Diff 消息、CoT 与溯源，并接收分层 ScopedClauseMemory 避免全量重载。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from .auditor import EnhancedRiskItem, PreciseCitation, ReasoningStep


class DiffMessage(BaseModel):
    """§2.4 DIFF 消息卡片"""

    type: str = "DIFF"
    before: str
    after: str
    clauseId: str


class RewriteResponse(BaseModel):
    """§8.4 AI 重写响应"""

    taskId: str
    clauseId: str
    before: str
    after: str
    diff: DiffMessage
    reasoningSteps: list[ReasoningStep] = Field(default_factory=list)
    citations: list[PreciseCitation] = Field(default_factory=list)
    updatedAt: str  # ISO 8601 UTC


class ScopedClauseMemory(BaseModel):
    """§10 条款级记忆（仅当前条款近 3-5 轮）"""

    targetClauseId: str
    recentDialog: list[dict] = Field(default_factory=list)
    draftAfter: str | None = None


class AgentRewriteRequest(BaseModel):
    """§10 Agent 重写请求（携带分层上下文）"""

    contractId: str
    clauseId: str
    instruction: str
    scopedMemory: ScopedClauseMemory
    ragContext: dict | None = None
    xRequestId: str  # 幂等


@dataclass
class RewriteContext:
    contract_id: str
    clause_id: str
    instruction: str
    memory: ScopedClauseMemory
    base_risks: list[EnhancedRiskItem] | None = None


def rewrite_clause(req: AgentRewriteRequest) -> RewriteResponse:
    """执行条款重写。

    TODO（工程实现）：调用 rewriter Agent（LangGraph），结合分层 Memory 与
    RAG 上下文，产出带溯源的改写结果。
    """
    raise NotImplementedError("rewriter.rewrite_clause 待实现：接入 LangGraph Agent")
