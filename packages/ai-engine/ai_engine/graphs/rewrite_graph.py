"""条款改写图（三阶段固定拓扑）。"""

from __future__ import annotations

import difflib
import time
from datetime import UTC, datetime
from functools import partial
from itertools import pairwise
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from ai_engine.agents.auditor import PreciseCitation, ReasoningStep
from ai_engine.graphs.deps import GraphDeps
from ai_engine.graphs.nodes import _finish_stage, _resolve_llm, _resolve_prompt
from ai_engine.graphs.state import RewriteState
from ai_engine.llm.structured import invoke_structured
from ai_engine.memory.scoped_memory import DialogTurn
from rag_contracts.enums import SourceType

REWRITE_STAGES: tuple[str, ...] = ("prepare", "rewrite", "finalize")


class _RewriteDraft(BaseModel):
    after: str
    reasoning_steps: list[ReasoningStep] = Field(default_factory=list)
    citations: list[PreciseCitation] = Field(default_factory=list)


def _recent_dialog(deps: GraphDeps) -> str:
    if deps.memory is None:
        return ""
    lines: list[str] = []
    for turn in deps.memory.recentDialog:
        if isinstance(turn, DialogTurn):
            role, content = turn.role, turn.content
        elif isinstance(turn, dict):
            role = str(turn.get("role", ""))
            content = str(turn.get("content", ""))
        else:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _rag_text(state: RewriteState) -> str:
    context = state.get("rag_context")
    if not isinstance(context, dict):
        return ""
    block = context.get("context_block")
    return str(block or "")


def prepare_node(state: RewriteState, deps: GraphDeps) -> dict[str, Any]:
    stage = REWRITE_STAGES[0]
    started = time.perf_counter()
    if isinstance(state.get("rag_context"), dict):
        return _finish_stage(
            state,
            stage,
            "OK",
            started,
            "沿用调用方 RAG 上下文",
            extra={"rag_context": state["rag_context"]},
        )
    if deps.rag_tool is None:
        warning = "RAG 工具不可用，继续无依据改写"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            warning,
            warnings=[warning],
            extra={"rag_context": None},
        )

    try:
        result = deps.rag_tool(
            query=state.get("before", ""),
            source_types=[
                SourceType.STANDARD_CLAUSE,
                SourceType.LAW_ARTICLE,
            ],
            top_k=3,
            reason="rewrite",
            trace_id=str(state.get("trace_id", "") or "-"),
        )
        context = {
            "status": result.status,
            "context_block": result.context_block,
            "citations": [
                citation.model_dump(mode="json") for citation in result.citations
            ],
        }
        status = "DEGRADED" if result.status in {"EMPTY", "DEGRADED"} else "OK"
        return _finish_stage(
            state,
            stage,
            status,
            started,
            f"RAG 上下文准备完成：{result.status}",
            warnings=result.warnings,
            extra={"rag_context": context},
        )
    except Exception as exc:  # noqa: BLE001 - RAG 失败必须降级继续
        warning = f"RAG 上下文准备失败：{type(exc).__name__}"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            warning,
            warnings=[warning],
            extra={"rag_context": None},
        )


def rewrite_node(state: RewriteState, deps: GraphDeps) -> dict[str, Any]:
    stage = REWRITE_STAGES[1]
    started = time.perf_counter()
    prompt = _resolve_prompt(deps, "rewrite")
    if prompt is None:
        warning = "缺少 rewrite 模板"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            warning,
            warnings=[warning],
        )

    try:
        model = _resolve_llm(deps, "rewrite")
        rendered = prompt.render(
            instruction=state.get("instruction", ""),
            before=state.get("before", ""),
            recent_dialog=_recent_dialog(deps),
            rag_context=_rag_text(state),
        )
        value, warnings = invoke_structured(
            model,
            _RewriteDraft,
            [HumanMessage(content=rendered)],
            trace_id=str(state.get("trace_id", "") or "-"),
        )
        if value is None:
            return _finish_stage(
                state,
                stage,
                "DEGRADED",
                started,
                "条款改写降级：模型未返回有效结果",
                warnings=warnings,
            )
        rag_context = state.get("rag_context")
        rag_citations = (
            rag_context.get("citations", []) if isinstance(rag_context, dict) else []
        )
        citations = value.citations if rag_citations else []
        return _finish_stage(
            state,
            stage,
            "DEGRADED" if warnings else "OK",
            started,
            "条款改写完成",
            warnings=warnings,
            extra={
                "after": value.after,
                "reasoning_steps": [
                    step.model_dump(mode="json") for step in value.reasoning_steps
                ],
                "citations": [
                    citation.model_dump(mode="json") for citation in citations
                ],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 失败由 rewrite_clause 统一抛出
        warning = f"条款改写失败：{type(exc).__name__}"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            warning,
            warnings=[warning],
        )


def finalize_node(state: RewriteState, deps: GraphDeps) -> dict[str, Any]:
    stage = REWRITE_STAGES[2]
    started = time.perf_counter()
    before = state.get("before", "")
    after = state.get("after", "")
    if not after:
        warning = "改写结果为空，无法生成 Diff"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            warning,
            warnings=[warning],
        )

    diff_text = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            lineterm="",
        )
    )
    diff = {
        "type": "DIFF",
        "before": before,
        "after": after,
        "clauseId": state.get("clause_id", ""),
        "text": diff_text,
        "updatedAt": datetime.now(UTC).isoformat(),
    }
    if deps.memory is not None:
        now = datetime.now(UTC).isoformat()
        turns = list(deps.memory.recentDialog)
        turns.extend(
            [
                DialogTurn(
                    role="USER", content=state.get("instruction", ""), createdAt=now
                ),
                DialogTurn(role="ASSISTANT", content=after, createdAt=now),
            ]
        )
        deps.memory.recentDialog = turns[-5:]
        deps.memory.draftAfter = after

    return _finish_stage(
        state,
        stage,
        "OK",
        started,
        "Diff 生成完成",
        extra={"diff": diff},
    )


def build_rewrite_graph(
    deps: GraphDeps,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    graph = StateGraph(RewriteState)
    nodes = (
        prepare_node,
        rewrite_node,
        finalize_node,
    )
    for stage, node in zip(REWRITE_STAGES, nodes, strict=True):
        graph.add_node(stage, partial(node, deps=deps))
    graph.add_edge(START, REWRITE_STAGES[0])
    for current_stage, next_stage in pairwise(REWRITE_STAGES):
        graph.add_edge(current_stage, next_stage)
    graph.add_edge(REWRITE_STAGES[-1], END)
    return graph.compile(checkpointer=checkpointer)


__all__ = ["REWRITE_STAGES", "build_rewrite_graph"]
