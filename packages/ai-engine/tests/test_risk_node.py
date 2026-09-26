"""风险节点单测（T-B3 / T-B4 / C6）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_engine.agents.auditor import (
    ReasoningStep,
    RiskCategory,
    RiskLevel,
)
from ai_engine.graphs import GraphDeps, new_audit_state, nodes
from ai_engine.graphs.nodes import _RiskDraft, risk_node
from ai_engine.prompts.loader import PromptLibrary
from rag_contracts.agent_tool import EMPTY_CONTEXT_HINT, AgentRAGToolResult
from rag_contracts.query import CitationOut


def _clause(index: int = 1, bbox: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "clause_id": f"c{index}",
        "label": f"第 {index} 条",
        "text": "甲方应在 30 日内付款。",
        "level": 0,
        "parent_id": None,
        "page_no": 1,
        "bbox": bbox,
    }


def _prompt_library(tmp_path: Path) -> PromptLibrary:
    (tmp_path / "risk.prompt").write_text(
        "{{clause_label}} {{clause_text}} {{contract_type}} "
        "{{tolerance}} {{rag_context}}",
        encoding="utf-8",
    )
    return PromptLibrary(str(tmp_path))


def _draft() -> _RiskDraft:
    return _RiskDraft(
        category=RiskCategory.PAYMENT_TERM_RISK,
        level=RiskLevel.HIGH,
        title="付款期限风险",
        suggestion="明确付款节点。",
        reasoning_steps=[
            ReasoningStep(
                stepNumber=1,
                thought="付款周期过长",
                evidenceText="30 日",
            )
        ],
    )


def _citation() -> CitationOut:
    return CitationOut(
        sourceType="STANDARD_CLAUSE",
        sourceTitle="标准付款条款",
        contentChunk="建议约定 15 日内付款。",
        relevanceScore=0.9,
    )


def _rag_result(
    *,
    status: str = "OK",
    context_block: str = "RAG 上下文",
    citations: list[CitationOut] | None = None,
) -> AgentRAGToolResult:
    return AgentRAGToolResult(
        status=status,
        context_block=context_block,
        citations=citations or [],
        trace_id="t1",
    )


class _RagStub:
    def __init__(
        self,
        *,
        result: AgentRAGToolResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, query: str, **kwargs: Any) -> AgentRAGToolResult:
        self.calls.append({"query": query, **kwargs})
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _state(
    *,
    clauses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return new_audit_state("C1", clauses or [], trace_id="t1")  # type: ignore[arg-type]


def test_risk_calls_once_per_clause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    result = risk_node(
        _state(clauses=[_clause(1), _clause(2)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    assert len(rag.calls) == 2
    assert result["rag_calls"] == 2
    assert result["llm_calls"] == 2
    assert len(result["risks"]) == 2


def test_risk_backfills_citations_and_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    result = risk_node(
        _state(clauses=[_clause(1)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    risk = result["risks"][0]
    assert risk.id == "C1:c1:risk:1"
    assert risk.clauseId == "c1"
    assert risk.originalText == "甲方应在 30 日内付款。"
    assert risk.citations[0].sourceTitle == "标准付款条款"


def test_risk_bbox_none_becomes_empty_dict_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    result = risk_node(
        _state(clauses=[_clause(1)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    assert result["risks"][0].bbox == {}
    assert any("缺少 bbox" in warning for warning in result["warnings"])


def test_rag_failure_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(error=RuntimeError("rag down"))
    captured: list[str] = []

    def _invoke(
        model: Any,
        schema: type[Any],
        messages: list[Any],
        **kwargs: Any,
    ) -> tuple[Any, list[str]]:
        captured.append(messages[0].content)
        return _draft(), []

    monkeypatch.setattr(nodes, "invoke_structured", _invoke)
    result = risk_node(
        _state(clauses=[_clause(1)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert result["rag_calls"] == 1
    assert len(result["risks"]) == 1
    assert EMPTY_CONTEXT_HINT in captured[0]


def test_llm_failure_skips_only_one_clause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    calls = 0

    def _invoke(*args: Any, **kwargs: Any) -> tuple[Any, list[str]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None, ["first failed"]
        return _draft(), []

    monkeypatch.setattr(nodes, "invoke_structured", _invoke)
    result = risk_node(
        _state(clauses=[_clause(1), _clause(2)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    assert result["llm_calls"] == 2
    assert len(result["risks"]) == 1
    assert result["risks"][0].clauseId == "c2"


def test_empty_recall_uses_exact_contract_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(
        result=_rag_result(
            status="EMPTY",
            context_block=EMPTY_CONTEXT_HINT,
        )
    )
    captured: list[str] = []

    def _invoke(*args: Any, **kwargs: Any) -> tuple[Any, list[str]]:
        captured.append(args[2][0].content)
        return _draft(), []

    monkeypatch.setattr(nodes, "invoke_structured", _invoke)
    result = risk_node(
        _state(clauses=[_clause(1)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    assert EMPTY_CONTEXT_HINT in captured[0]
    assert result["stage_log"][0]["status"] == "OK"


def test_missing_prompt_skips_without_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result())
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: pytest.fail("缺模板时不得调用 LLM"),
    )
    result = risk_node(
        _state(clauses=[_clause(1)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=PromptLibrary(None),
            rag_tool=rag,
        ),
    )
    assert result["stage_log"][0]["status"] == "SKIPPED"
    assert result["rag_calls"] == 0
    assert result["llm_calls"] == 0
    assert rag.calls == []


def test_empty_clauses_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: pytest.fail("无条款时不得调用 LLM"),
    )
    result = risk_node(_state(), GraphDeps())
    assert result["stage_log"][0]["status"] == "SKIPPED"
    assert result["risks"] == []


def test_budget_truncates_clauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    result = risk_node(
        _state(clauses=[_clause(index) for index in range(150)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
            max_clauses=100,
        ),
    )
    assert len(rag.calls) == 100
    assert result["rag_calls"] == 100
    assert any("条款数 150 超过上限 100" in warning for warning in result["warnings"])


def test_stage_log_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_draft(), []),
    )
    result = risk_node(
        _state(clauses=[_clause(1)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            rag_tool=rag,
        ),
    )
    assert len(result["stage_log"]) == 1
    assert result["stage_log"][0]["stage"] == "risk"
