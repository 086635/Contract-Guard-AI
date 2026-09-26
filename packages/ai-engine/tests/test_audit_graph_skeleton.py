"""审核图骨架与依赖装配单测（T-B3 / C4）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from langgraph.graph import StateGraph

from ai_engine.graphs import (
    AUDIT_STAGES,
    GraphDeps,
    build_audit_graph,
    new_audit_state,
)
from ai_engine.graphs.nodes import _bounded_clauses

_EXPECTED_NODES = {
    "classify",
    "extract",
    "risk",
    "conflict",
    "standard_compare",
    "report_draft",
}


def _clause(index: int) -> dict[str, Any]:
    return {
        "clause_id": f"c{index}",
        "label": f"第 {index} 条",
        "text": "合同正文",
        "level": 0,
        "parent_id": None,
        "page_no": 1,
        "bbox": None,
    }


def test_graph_deps_defaults() -> None:
    deps = GraphDeps()
    assert deps.rag_tool is None
    assert deps.llm is None
    assert deps.prompt_lib is None
    assert deps.max_clauses == 100
    assert deps.max_conflict_pairs == 50
    assert deps.conflict_batch_size == 10


def test_graph_has_exact_six_nodes() -> None:
    graph = build_audit_graph(GraphDeps()).get_graph()
    assert set(graph.nodes) == _EXPECTED_NODES | {"__start__", "__end__"}
    assert set(AUDIT_STAGES) == _EXPECTED_NODES


def test_stream_order_is_fixed() -> None:
    graph = build_audit_graph(GraphDeps())
    state = new_audit_state("C1", [], trace_id="t1")
    seen = [
        name for chunk in graph.stream(state, stream_mode="updates") for name in chunk
    ]
    assert seen == list(AUDIT_STAGES)


def test_graph_run_records_six_stage_logs() -> None:
    graph = build_audit_graph(GraphDeps())
    result = graph.invoke(new_audit_state("C1", [], trace_id="t1"))
    assert [item["stage"] for item in result["stage_log"]] == list(AUDIT_STAGES)
    assert all(
        item["status"] in {"OK", "SKIPPED", "DEGRADED"} for item in result["stage_log"]
    )
    assert result["stage_log"][-1]["status"] == "OK"
    assert result["health_score"] == 100


def test_bounded_clauses_truncates_with_warning() -> None:
    state = new_audit_state(
        "C1",
        [_clause(index) for index in range(150)],
        trace_id="t1",
    )
    clauses, warnings = _bounded_clauses(state, GraphDeps(max_clauses=100))
    assert len(clauses) == 100
    assert warnings == ["条款数 150 超过上限 100，仅审核前 100 条"]


def test_frozen_deps_cannot_mutate() -> None:
    deps = GraphDeps()
    with pytest.raises(FrozenInstanceError):
        deps.max_clauses = 1  # type: ignore[misc]


def test_no_model_constructed_in_nodes_source() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "ai_engine" / "graphs" / "nodes.py"
    ).read_text(encoding="utf-8")
    assert "ChatOpenAI(" not in source
    assert "OpenAI(" not in source


def test_checkpointer_is_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    captured: dict[str, Any] = {}

    def _fake_compile(
        self: StateGraph,
        checkpointer: object = None,
        **kwargs: Any,
    ) -> object:
        captured["checkpointer"] = checkpointer
        captured["kwargs"] = kwargs
        return marker

    monkeypatch.setattr(StateGraph, "compile", _fake_compile)
    result = build_audit_graph(GraphDeps(), checkpointer=marker)
    assert result is marker
    assert captured["checkpointer"] is marker
