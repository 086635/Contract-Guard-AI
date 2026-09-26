"""图状态结构与 reducer 单测（T-B2 / C3）。"""

from __future__ import annotations

import ast
import json
import operator
import typing
from pathlib import Path

import pytest
from langgraph.channels import BinaryOperatorAggregate

from ai_engine.graphs import (
    AUDIT_STAGES,
    AuditState,
    ClauseInput,
    StageLog,
    new_audit_state,
)

_AUDIT_KEYS = {
    "contract_id",
    "trace_id",
    "tolerance",
    "clauses",
    "contract_type",
    "elements",
    "risks",
    "conflicts",
    "missing_clauses",
    "report_draft",
    "health_score",
    "warnings",
    "stage_log",
    "llm_calls",
    "rag_calls",
}
_APPEND_KEYS = {
    "risks",
    "conflicts",
    "missing_clauses",
    "warnings",
    "stage_log",
    "llm_calls",
    "rag_calls",
}
_CLAUSE_KEYS = {
    "clause_id",
    "label",
    "text",
    "level",
    "parent_id",
    "page_no",
    "bbox",
}


def _clause(**overrides: object) -> ClauseInput:
    clause: ClauseInput = {
        "clause_id": "c1",
        "label": "第一条",
        "text": "合同正文",
        "level": 0,
        "parent_id": None,
        "page_no": 1,
        "bbox": None,
    }
    clause.update(overrides)  # type: ignore[typeddict-item]
    return clause


def _state_source() -> str:
    return (
        Path(__file__).resolve().parents[1] / "ai_engine" / "graphs" / "state.py"
    ).read_text(encoding="utf-8")


def test_audit_state_keys_are_frozen() -> None:
    assert set(AuditState.__annotations__) == _AUDIT_KEYS


def test_only_append_fields_have_reducer() -> None:
    hints = typing.get_type_hints(AuditState, include_extras=True)
    append_fields = {
        key
        for key, value in hints.items()
        if getattr(value, "__metadata__", ()) and operator.add in value.__metadata__
    }
    assert append_fields == _APPEND_KEYS


def test_audit_stages_order() -> None:
    assert AUDIT_STAGES == (
        "classify",
        "extract",
        "risk",
        "conflict",
        "standard_compare",
        "report_draft",
    )


def test_new_audit_state_injects_defaults() -> None:
    state = new_audit_state("C1", [], trace_id="t1")
    assert state["contract_id"] == "C1"
    assert state["trace_id"] == "t1"
    assert state["tolerance"] == "BALANCED"
    assert state["warnings"] == []
    assert state["stage_log"] == []
    assert state["llm_calls"] == 0
    assert state["rag_calls"] == 0


def test_new_audit_state_requires_trace_id() -> None:
    with pytest.raises(TypeError):
        new_audit_state("C1", [])  # type: ignore[call-arg]


def test_empty_clauses_is_legal() -> None:
    state = new_audit_state("C1", [], trace_id="t")
    assert state["clauses"] == []


def test_state_is_json_serializable() -> None:
    state = new_audit_state("C1", [_clause()], trace_id="t1")
    restored = json.loads(json.dumps(state))
    assert restored == state


def test_clause_input_keys() -> None:
    assert set(ClauseInput.__annotations__) == _CLAUSE_KEYS


def test_bbox_none_by_default() -> None:
    clause: ClauseInput = {
        "clause_id": "c1",
        "label": "第一条",
        "text": "合同正文",
        "level": 0,
        "parent_id": None,
        "page_no": None,
        "bbox": None,
    }
    state = new_audit_state("C1", [clause], trace_id="t")
    assert state["clauses"][0]["bbox"] is None


def test_stage_log_shape() -> None:
    assert set(StageLog.__annotations__) == {
        "stage",
        "status",
        "latency_ms",
        "detail",
    }


def test_no_dependency_objects_in_state() -> None:
    source = _state_source()
    assert "doc_processor" not in source

    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden_prefixes = ("rag_core", "langchain", "ai_engine.llm")
    assert not any(
        module.startswith(prefix)
        for module in imported_modules
        for prefix in forbidden_prefixes
    )


def test_append_reducers_merge_parallel_updates() -> None:
    warnings_channel: BinaryOperatorAggregate[list[str]] = BinaryOperatorAggregate(
        list,
        operator.add,
    )
    warnings_channel.update([["warning-a"], ["warning-b"]])
    assert warnings_channel.get() == ["warning-a", "warning-b"]

    calls_channel: BinaryOperatorAggregate[int] = BinaryOperatorAggregate(
        int,
        operator.add,
    )
    calls_channel.update([1, 1, 1])
    assert calls_channel.get() == 3
