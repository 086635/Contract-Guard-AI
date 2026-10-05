"""矛盾与标准比对节点单测（T-B3 / C7）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_engine.agents.auditor import RiskLevel
from ai_engine.agents.conflict import ConflictType
from ai_engine.graphs import GraphDeps, new_audit_state, nodes
from ai_engine.graphs.nodes import (
    _ConflictBatch,
    _ConflictDraft,
    build_conflict_candidates,
    conflict_node,
    standard_compare_node,
)
from ai_engine.prompts.loader import PromptLibrary
from rag_contracts.agent_tool import AgentRAGToolResult
from rag_contracts.enums import SourceType
from rag_contracts.query import CitationOut


def _clause(index: int, text: str) -> dict[str, Any]:
    return {
        "clause_id": f"c{index}",
        "label": f"第 {index + 1} 条",
        "text": text,
        "level": 0,
        "parent_id": None,
        "page_no": 1,
        "bbox": None,
    }


def _state(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    return new_audit_state("C1", clauses, trace_id="t1")  # type: ignore[arg-type]


def _prompt_library(tmp_path: Path) -> PromptLibrary:
    (tmp_path / "conflict.prompt").write_text(
        "{{pair_block}} {{contract_type}}",
        encoding="utf-8",
    )
    return PromptLibrary(str(tmp_path))


def _draft(
    pair_index: int,
    *,
    conflict_type: ConflictType = ConflictType.PAYMENT_CONTRADICTION,
) -> _ConflictDraft:
    return _ConflictDraft(
        pair_index=pair_index,
        conflict_type=conflict_type,
        description="付款期限冲突",
        level=RiskLevel.HIGH,
        suggestion="统一付款期限。",
    )


def _citation() -> CitationOut:
    return CitationOut(
        sourceType=SourceType.STANDARD_CLAUSE,
        sourceTitle="标准条款",
        contentChunk="标准条款正文",
        relevanceScore=0.9,
    )


def _rag_result(
    *,
    status: str = "OK",
    context_block: str = "标准依据",
    citations: list[CitationOut] | None = None,
) -> AgentRAGToolResult:
    return AgentRAGToolResult(
        status=status,
        context_block=context_block,
        citations=citations or [],
        trace_id="t1",
    )


def test_official_templates_are_not_enterprise_approved_baselines() -> None:
    citation = _citation().model_copy(
        update={"sourceTitle": "官方范本 GF—2025—0151 · 第六条"}
    )
    stub = _RagStub(result=_rag_result(citations=[citation]))
    result = standard_compare_node(
        _state([_clause(0, "买方应支付价款")]), GraphDeps(rag_tool=stub)
    )
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert result["missing_clauses"] == []
    assert stub.calls[0]["source_types"] == [SourceType.STANDARD_CLAUSE]
    assert any("企业确认" in warning for warning in result["warnings"])


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


def test_candidate_keyword_filter() -> None:
    candidates = build_conflict_candidates(
        [_clause(0, "付款期限"), _clause(1, "知识产权归属")],
        50,
    )
    assert candidates == []


def test_candidate_deduplicates_pairs() -> None:
    clauses = [
        _clause(0, "付款 支付 价款 结算"),
        _clause(1, "付款 支付 价款 结算"),
    ]
    candidates = build_conflict_candidates(clauses, 50)
    assert len(candidates) == 1


def test_candidate_sorted_by_span() -> None:
    clauses = [
        _clause(0, "付款"),
        _clause(1, "付款"),
        _clause(2, "无关"),
        _clause(3, "付款"),
    ]
    candidates = build_conflict_candidates(clauses, 50)
    pairs = [(left["clause_id"], right["clause_id"]) for left, right in candidates]
    assert pairs == [
        ("c0", "c1"),
        ("c1", "c3"),
        ("c0", "c3"),
    ]


def test_candidate_cap_is_50() -> None:
    clauses = [_clause(index, "付款 支付 价款") for index in range(100)]
    candidates = build_conflict_candidates(clauses, 50)
    assert len(candidates) == 50


def test_conflict_batch_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _empty_batch(*args: Any, **kwargs: Any) -> tuple[_ConflictBatch, list[str]]:
        nonlocal calls
        calls += 1
        return _ConflictBatch(), []

    monkeypatch.setattr(nodes, "invoke_structured", _empty_batch)
    result = conflict_node(
        _state([_clause(index, "付款 支付 价款") for index in range(100)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            max_conflict_pairs=50,
            conflict_batch_size=10,
        ),
    )
    assert calls == 5
    assert result["llm_calls"] == 5


def test_conflict_backfills_ids_from_pair_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_ConflictBatch(conflicts=[_draft(2)]), []),
    )
    result = conflict_node(
        _state([_clause(index, "付款 支付 价款") for index in range(3)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            max_conflict_pairs=50,
        ),
    )
    conflict = result["conflicts"][0]
    assert conflict.id == "C1:conflict:1"
    assert conflict.sourceClauseId == "c0"
    assert conflict.targetClauseId == "c2"


def test_conflict_batch_failure_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _invoke(*args: Any, **kwargs: Any) -> tuple[_ConflictBatch, list[str]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("batch failed")
        return _ConflictBatch(conflicts=[_draft(10)]), []

    monkeypatch.setattr(nodes, "invoke_structured", _invoke)
    result = conflict_node(
        _state([_clause(index, "付款 支付 价款") for index in range(6)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_prompt_library(tmp_path),
            max_conflict_pairs=15,
            conflict_batch_size=10,
        ),
    )
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0].sourceClauseId == "c1"
    assert result["conflicts"][0].targetClauseId == "c4"


def test_empty_recall_cannot_prove_missing() -> None:
    rag = _RagStub(
        result=_rag_result(
            status="EMPTY",
            context_block="知识库无对应标准条款",
            citations=[],
        )
    )
    result = standard_compare_node(
        _state([_clause(0, "付款期限")]),
        GraphDeps(rag_tool=rag),
    )
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert result["missing_clauses"] == []
    assert any("无法判定缺失条款" in warning for warning in result["warnings"])


def test_tool_unavailable_not_missing() -> None:
    result = standard_compare_node(
        _state([_clause(0, "付款期限")]),
        GraphDeps(rag_tool=None),
    )
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert result["missing_clauses"] == []
    assert result["rag_calls"] == 0


def test_rag_exception_not_missing() -> None:
    rag = _RagStub(error=RuntimeError("rag down"))
    result = standard_compare_node(
        _state([_clause(0, "付款期限")]),
        GraphDeps(rag_tool=rag),
    )
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert result["missing_clauses"] == []
    assert result["rag_calls"] == 1


def test_standard_compare_calls_once_per_clause() -> None:
    rag = _RagStub(result=_rag_result(citations=[_citation()]))
    result = standard_compare_node(
        _state([_clause(0, "付款期限"), _clause(1, "违约责任")]),
        GraphDeps(rag_tool=rag),
    )
    assert len(rag.calls) == 2
    assert result["rag_calls"] == 2
    assert result["missing_clauses"] == []


def test_empty_recall_does_not_fabricate_missing_fields_or_repeat_warnings() -> None:
    rag = _RagStub(result=_rag_result(status="EMPTY", context_block="无依据"))
    result = standard_compare_node(
        _state([_clause(0, "付款期限"), _clause(1, "违约责任")]),
        GraphDeps(rag_tool=rag),
    )
    assert result["missing_clauses"] == []
    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert result["rag_calls"] == 2
    assert len(result["warnings"]) == 1
