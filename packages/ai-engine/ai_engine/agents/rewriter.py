"""条款重写 Agent（rewriter）。

职责：依据自然语言指令改写条款，产出 RewriteResponse（§8.4），包含
before/after、Diff 消息、CoT 与溯源，并接收分层 ScopedClauseMemory 避免全量重载。
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock

from pydantic import BaseModel, Field

from ai_engine.graphs.deps import GraphDeps
from ai_engine.graphs.state import RewriteState
from ai_engine.memory import ScopedClauseMemory
from ai_engine.prompts import load_default_library

from .auditor import EnhancedRiskItem, PreciseCitation, ReasoningStep
from .errors import RewriteUnavailableError
from .tools import build_rag_tool


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

    使用三阶段 `rewrite_graph`，按 `xRequestId` 做有界幂等缓存。
    """
    cached = _cache_get(req.xRequestId)
    if cached is not None:
        return cached

    before = _before_text(req)
    memory = req.scopedMemory
    trace_id = req.xRequestId or uuid.uuid4().hex
    deps = GraphDeps(
        rag_tool=build_rag_tool(),
        llm=None,
        prompt_lib=load_default_library(),
        memory=memory,
    )
    state: RewriteState = {
        "contract_id": req.contractId,
        "clause_id": req.clauseId,
        "trace_id": trace_id,
        "instruction": req.instruction,
        "before": before,
        "rag_context": req.ragContext,
        "warnings": [],
        "stage_log": [],
    }
    config = {
        "configurable": {
            "thread_id": f"{req.contractId}:{req.clauseId}",
        }
    }

    try:
        from ai_engine.graphs.rewrite_graph import build_rewrite_graph

        final_state = build_rewrite_graph(deps).invoke(  # type: ignore[call-overload]
            state,
            config=config,
        )
        after = str(final_state.get("after", "") or "")
        diff_payload = final_state.get("diff")
        if not after or not isinstance(diff_payload, dict):
            raise RewriteUnavailableError(
                "改写模型不可用",
                trace_id=trace_id,
            )
        response = RewriteResponse(
            taskId=req.xRequestId,
            clauseId=req.clauseId,
            before=before,
            after=after,
            diff=DiffMessage.model_validate(diff_payload),
            reasoningSteps=[
                ReasoningStep.model_validate(item)
                for item in final_state.get("reasoning_steps", [])
            ],
            citations=[
                PreciseCitation.model_validate(item)
                for item in final_state.get("citations", [])
            ],
            updatedAt=diff_payload.get("updatedAt", ""),
        )
    except RewriteUnavailableError:
        raise
    except Exception as exc:
        raise RewriteUnavailableError(
            f"改写失败：{type(exc).__name__}",
            trace_id=trace_id,
        ) from exc

    _cache_put(req.xRequestId, response)
    return response


_MAX_IDEMPOTENCY = 256
_IDEMPOTENCY: OrderedDict[str, RewriteResponse] = OrderedDict()
_IDEMPOTENCY_LOCK = RLock()


def _before_text(req: AgentRewriteRequest) -> str:
    rag_context = req.ragContext
    if isinstance(rag_context, dict):
        candidate = rag_context.get("before")
        if isinstance(candidate, str) and candidate:
            return candidate
    return req.scopedMemory.draftAfter or ""


def _cache_get(key: str) -> RewriteResponse | None:
    with _IDEMPOTENCY_LOCK:
        value = _IDEMPOTENCY.get(key)
        if value is not None:
            _IDEMPOTENCY.move_to_end(key)
        return value


def _cache_put(key: str, value: RewriteResponse) -> None:
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY[key] = value
        _IDEMPOTENCY.move_to_end(key)
        while len(_IDEMPOTENCY) > _MAX_IDEMPOTENCY:
            _IDEMPOTENCY.popitem(last=False)


def _clear_rewrite_cache() -> None:
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY.clear()
