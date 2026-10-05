"""改写图单测（T-B5 / C11）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ai_engine.agents.auditor import PreciseCitation, ReasoningStep
from ai_engine.agents.errors import RewriteUnavailableError
from ai_engine.agents.rewriter import (
    _IDEMPOTENCY,
    AgentRewriteRequest,
    ScopedClauseMemory,
    _cache_put,
    _clear_rewrite_cache,
    rewrite_clause,
)
from ai_engine.graphs import REWRITE_STAGES, GraphDeps, build_rewrite_graph
from ai_engine.graphs import rewrite_graph as rewrite_graph_module
from ai_engine.memory.scoped_memory import ScopedClauseMemory as MemorySnapshot
from ai_engine.prompts.loader import PromptLibrary


def _request(request_id: str = "req-1") -> AgentRewriteRequest:
    return AgentRewriteRequest(
        contractId="C1",
        clauseId="c1",
        instruction="将付款期限改为 15 日",
        scopedMemory=ScopedClauseMemory(
            targetClauseId="c1",
            draftAfter="甲方应在 30 日内付款。",
        ),
        ragContext=None,
        xRequestId=request_id,
    )


def _draft() -> Any:
    return rewrite_graph_module._RewriteDraft(
        after="甲方应在 15 日内付款。",
        reasoning_steps=[
            ReasoningStep(
                stepNumber=1,
                thought="缩短付款期限",
                evidenceText="30 日",
            )
        ],
        citations=[
            PreciseCitation(
                sourceType="STANDARD_CLAUSE",
                sourceTitle="标准付款条款",
                contentChunk="15 日内付款",
                relevanceScore=0.9,
            )
        ],
    )


def _prompt_library(tmp_path: Path) -> PromptLibrary:
    (tmp_path / "rewrite.prompt").write_text(
        "{{instruction}} {{before}} {{recent_dialog}} {{rag_context}}",
        encoding="utf-8",
    )
    return PromptLibrary(str(tmp_path))


class _FakeRewriteGraph:
    def __init__(
        self,
        *,
        success: bool = True,
        captured: dict[str, Any] | None = None,
    ) -> None:
        self.success = success
        self.calls = 0
        self.captured = captured if captured is not None else {}

    def invoke(
        self, state: dict[str, Any], *, config: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls += 1
        self.captured["config"] = config
        if not self.success:
            return {**state, "after": ""}
        return {
            **state,
            "after": "甲方应在 15 日内付款。",
            "reasoning_steps": [
                {
                    "stepNumber": 1,
                    "thought": "缩短付款期限",
                    "evidenceText": "30 日",
                }
            ],
            "citations": [
                {
                    "sourceType": "STANDARD_CLAUSE",
                    "sourceTitle": "标准付款条款",
                    "contentChunk": "15 日内付款",
                    "relevanceScore": 0.9,
                }
            ],
            "diff": {
                "type": "DIFF",
                "before": state["before"],
                "after": "甲方应在 15 日内付款。",
                "clauseId": "c1",
                "updatedAt": datetime.now(UTC).isoformat(),
            },
        }


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    _clear_rewrite_cache()
    yield
    _clear_rewrite_cache()


def test_rewrite_end_to_end_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _FakeRewriteGraph()
    monkeypatch.setattr(
        rewrite_graph_module, "invoke_structured", lambda *a, **k: (_draft(), [])
    )
    monkeypatch.setattr(
        "ai_engine.graphs.rewrite_graph.build_rewrite_graph",
        lambda deps: graph,
    )
    monkeypatch.setattr("ai_engine.agents.rewriter.build_rag_tool", lambda: object())
    monkeypatch.setattr(
        "ai_engine.agents.rewriter.load_default_library",
        lambda: object(),
    )
    response = rewrite_clause(_request())
    assert response.before == "甲方应在 30 日内付款。"
    assert response.after == "甲方应在 15 日内付款。"
    assert response.diff.type == "DIFF"
    assert response.diff.clauseId == "c1"
    assert response.reasoningSteps
    assert response.citations


def test_rewrite_normalizes_llm_citation_source_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _FakeRewriteGraph()
    draft = _draft()
    draft.citations[0].sourceType = "知识库检索结果"
    monkeypatch.setattr(
        rewrite_graph_module, "invoke_structured", lambda *a, **k: (draft, [])
    )
    monkeypatch.setattr(
        "ai_engine.graphs.rewrite_graph.build_rewrite_graph",
        lambda deps: graph,
    )
    monkeypatch.setattr("ai_engine.agents.rewriter.build_rag_tool", lambda: object())
    monkeypatch.setattr(
        "ai_engine.agents.rewriter.load_default_library",
        lambda: object(),
    )

    response = rewrite_clause(_request())

    assert response.citations[0].sourceType == "STANDARD_CLAUSE"


def test_thread_id_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "ai_engine.graphs.rewrite_graph.build_rewrite_graph",
        lambda deps: _FakeRewriteGraph(captured=captured),
    )
    monkeypatch.setattr("ai_engine.agents.rewriter.build_rag_tool", lambda: object())
    monkeypatch.setattr(
        "ai_engine.agents.rewriter.load_default_library",
        lambda: object(),
    )
    rewrite_clause(_request())
    assert captured["config"]["configurable"]["thread_id"] == "C1:c1"


def test_idempotent_by_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _FakeRewriteGraph()
    monkeypatch.setattr(
        "ai_engine.graphs.rewrite_graph.build_rewrite_graph",
        lambda deps: graph,
    )
    monkeypatch.setattr("ai_engine.agents.rewriter.build_rag_tool", lambda: object())
    monkeypatch.setattr(
        "ai_engine.agents.rewriter.load_default_library",
        lambda: object(),
    )
    first = rewrite_clause(_request("same"))
    second = rewrite_clause(_request("same"))
    assert first is second
    assert graph.calls == 1


def test_idempotency_cache_bounded() -> None:
    response = _FakeRewriteGraph().invoke(
        {
            "before": "before",
            "after": "after",
            "reasoning_steps": [],
            "citations": [],
            "diff": {
                "type": "DIFF",
                "before": "before",
                "after": "after",
                "clauseId": "c1",
                "updatedAt": datetime.now(UTC).isoformat(),
            },
        },
        config={"configurable": {"thread_id": "C1:c1"}},
    )
    from ai_engine.agents.rewriter import RewriteResponse

    parsed = RewriteResponse.model_validate(
        {
            "taskId": "task",
            "clauseId": "c1",
            "before": "before",
            "after": "after",
            "diff": response["diff"],
            "reasoningSteps": [],
            "citations": [],
            "updatedAt": response["diff"]["updatedAt"],
        }
    )
    for index in range(300):
        _cache_put(str(index), parsed)
    assert len(_IDEMPOTENCY) == 256


def test_diff_shape_matches_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rewrite_graph_module,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    graph = build_rewrite_graph(
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=None,
        )
    )
    state = graph.invoke(
        {
            "contract_id": "C1",
            "clause_id": "c1",
            "trace_id": "t1",
            "instruction": "改为 15 日",
            "before": "甲方应在 30 日内付款。",
            "warnings": [],
            "stage_log": [],
        }
    )
    diff = state["diff"]
    assert set(diff) >= {"type", "before", "after", "clauseId"}
    assert diff["before"] == "甲方应在 30 日内付款。"
    assert diff["after"] == "甲方应在 15 日内付款。"


def test_rag_unavailable_still_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rewrite_graph_module,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    graph = build_rewrite_graph(
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=None,
        )
    )
    state = graph.invoke(
        {
            "contract_id": "C1",
            "clause_id": "c1",
            "trace_id": "t1",
            "instruction": "改为 15 日",
            "before": "甲方应在 30 日内付款。",
            "warnings": [],
            "stage_log": [],
        }
    )
    assert state["after"] == "甲方应在 15 日内付款。"
    assert state["citations"]


def test_llm_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_engine.graphs.rewrite_graph.build_rewrite_graph",
        lambda deps: _FakeRewriteGraph(success=False),
    )
    monkeypatch.setattr("ai_engine.agents.rewriter.build_rag_tool", lambda: object())
    monkeypatch.setattr(
        "ai_engine.agents.rewriter.load_default_library",
        lambda: object(),
    )
    with pytest.raises(RewriteUnavailableError):
        rewrite_clause(_request())


def test_failure_does_not_write_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rewrite_graph_module,
        "invoke_structured",
        lambda *args, **kwargs: (None, ["failed"]),
    )
    memory = MemorySnapshot(targetClauseId="c1", draftAfter="old")
    graph = build_rewrite_graph(
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=None,
            memory=memory,
        )
    )
    graph.invoke(
        {
            "contract_id": "C1",
            "clause_id": "c1",
            "trace_id": "t1",
            "instruction": "改为 15 日",
            "before": "甲方应在 30 日内付款。",
            "warnings": [],
            "stage_log": [],
        }
    )
    assert memory.recentDialog == []
    assert memory.draftAfter == "old"


def test_rewrite_stages() -> None:
    assert REWRITE_STAGES == ("prepare", "rewrite", "finalize")


def test_no_direct_ragservice_import() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "ai_engine"
        / "graphs"
        / "rewrite_graph.py"
    ).read_text(encoding="utf-8")
    assert "RAGService(" not in source


def test_original_text_parameters_do_not_bypass_rag():
    from types import SimpleNamespace

    captured = []
    def retrieve(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(status="OK", context_block="真实检索法条", citations=[], warnings=[])
    state = {"before": "付款条款原文", "rag_context": {"before": "付款条款原文", "relatedLaws": []}, "warnings": [], "stage_log": []}
    result = rewrite_graph_module.prepare_node(state, GraphDeps(rag_tool=retrieve))
    assert len(captured) == 1
    assert captured[0]["query"] == "付款条款原文"
    assert result["rag_context"]["context_block"] == "真实检索法条"


def test_nonempty_retrieved_context_is_reused():
    def unexpected_query(**kwargs):
        raise AssertionError("已有检索上下文不应重复召回")
    context = {"context_block": "已完成检索的法条", "citations": []}
    state = {"before": "原文", "rag_context": context, "warnings": [], "stage_log": []}
    result = rewrite_graph_module.prepare_node(state, GraphDeps(rag_tool=unexpected_query))
    assert result["rag_context"] == context


def test_empty_supplied_context_without_rag_reports_degradation():
    state = {"before": "原文", "rag_context": {"context_block": " "}, "warnings": [], "stage_log": []}
    result = rewrite_graph_module.prepare_node(state, GraphDeps(rag_tool=None))
    assert result["rag_context"] is None
    assert result["stage_log"][-1]["status"] == "DEGRADED"
