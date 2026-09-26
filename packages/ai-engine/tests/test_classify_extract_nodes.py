"""分类与要素节点单测（T-B3 / C5）。"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from ai_engine.graphs import GraphDeps, new_audit_state, nodes
from ai_engine.graphs.nodes import (
    _ClassifyOutput,
    _ElementDraft,
    _ExtractBatch,
    classify_node,
    extract_node,
)
from ai_engine.prompts.loader import PromptLibrary
from rag_contracts.enums import ContractType


def _clause(index: int = 1) -> dict[str, Any]:
    return {
        "clause_id": f"c{index}",
        "label": f"第 {index} 条",
        "text": "甲方应在 30 日内付款。",
        "level": 0,
        "parent_id": None,
        "page_no": 1,
        "bbox": None,
    }


def _write_prompt(tmp_path: Path, name: str, template: str) -> PromptLibrary:
    (tmp_path / f"{name}.prompt").write_text(template, encoding="utf-8")
    return PromptLibrary(str(tmp_path))


def _state(
    *,
    clauses: list[dict[str, Any]] | None = None,
    elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    state = new_audit_state("C1", clauses or [], trace_id="t1")  # type: ignore[arg-type]
    if elements is not None:
        state["elements"] = elements
    return state


def test_classify_success_writes_contract_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _write_prompt(
        tmp_path,
        "classify",
        "{{contract_title}} {{clause_excerpt}}",
    )
    captured: dict[str, Any] = {}

    def _fake_invoke(
        model: Any,
        schema: type[Any],
        messages: list[Any],
        **kwargs: Any,
    ) -> tuple[Any, list[str]]:
        captured["content"] = messages[0].content
        return _ClassifyOutput(contract_type=ContractType.NDA), []

    monkeypatch.setattr(nodes, "invoke_structured", _fake_invoke)
    result = classify_node(
        _state(clauses=[_clause()]),
        GraphDeps(llm=object(), prompt_lib=library),  # type: ignore[arg-type]
    )

    assert result["contract_type"] is ContractType.NDA
    assert result["llm_calls"] == 1
    assert result["stage_log"][0]["status"] == "OK"
    assert "C1" in captured["content"]
    assert "甲方应在 30 日内付款" in captured["content"]


def test_classify_missing_prompt_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("缺模板时不得调用 LLM")

    monkeypatch.setattr(nodes, "invoke_structured", _unexpected)
    result = classify_node(
        _state(clauses=[_clause()]),
        GraphDeps(prompt_lib=PromptLibrary(None)),
    )
    assert result["contract_type"] is None
    assert result["llm_calls"] == 0
    assert result["stage_log"][0]["status"] == "SKIPPED"
    assert any("缺少 classify 模板" in warning for warning in result["warnings"])


def test_classify_no_clauses_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: pytest.fail("无条款时不得调用 LLM"),
    )
    result = classify_node(_state(), GraphDeps())
    assert result["contract_type"] is None
    assert result["llm_calls"] == 0
    assert result["stage_log"][0]["status"] == "SKIPPED"


def test_classify_failure_degrades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _write_prompt(
        tmp_path,
        "classify",
        "{{contract_title}} {{clause_excerpt}}",
    )

    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(nodes, "invoke_structured", _fail)
    result = classify_node(
        _state(clauses=[_clause()]),
        GraphDeps(llm=object(), prompt_lib=library),  # type: ignore[arg-type]
    )
    assert result["contract_type"] is None
    assert result["llm_calls"] == 1
    assert result["stage_log"][0]["status"] == "DEGRADED"


def test_extract_elements_passthrough_normalizes_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicated = {
        "key": "party_a",
        "label": "甲方",
        "value": "示例公司",
        "confidence": 0.9,
        "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
        "isCustom": False,
    }
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: pytest.fail("已有 elements 时不得调用 LLM"),
    )

    result = extract_node(
        _state(elements=[duplicated, dict(duplicated)]),
        GraphDeps(),
    )
    assert result["llm_calls"] == 0
    assert result["stage_log"][0]["status"] == "OK"
    assert len(result["elements"]) == 1
    assert result["elements"][0]["key"] == "party_a"


def test_extract_element_shape_is_canonical() -> None:
    result = extract_node(
        _state(
            elements=[
                {
                    "key": "amount",
                    "value": 100,
                }
            ]
        ),
        GraphDeps(),
    )
    element = result["elements"][0]
    assert set(element) == {
        "key",
        "label",
        "value",
        "confidence",
        "bbox",
        "isCustom",
    }
    assert element["label"] == "amount"
    assert element["value"] == "100"
    assert element["confidence"] == 0.0
    assert element["bbox"] is None
    assert element["isCustom"] is False


def test_extract_llm_supplement_has_no_bbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _write_prompt(tmp_path, "extract", "{{clause_block}}")

    def _fake_invoke(*args: Any, **kwargs: Any) -> tuple[Any, list[str]]:
        return (
            _ExtractBatch(
                elements=[
                    _ElementDraft(
                        key="term",
                        label="合同期限",
                        value="一年",
                        confidence=0.88,
                    )
                ]
            ),
            [],
        )

    monkeypatch.setattr(nodes, "invoke_structured", _fake_invoke)
    result = extract_node(
        _state(clauses=[_clause()]),
        GraphDeps(llm=object(), prompt_lib=library),  # type: ignore[arg-type]
    )
    assert result["llm_calls"] == 1
    assert result["elements"][0]["bbox"] is None
    assert result["elements"][0]["isCustom"] is True


def test_extract_missing_prompt_returns_empty() -> None:
    result = extract_node(
        _state(clauses=[_clause()]),
        GraphDeps(prompt_lib=PromptLibrary(None)),
    )
    assert result["elements"] == []
    assert result["llm_calls"] == 0
    assert result["stage_log"][0]["status"] == "SKIPPED"
    assert any("缺少 extract 模板" in warning for warning in result["warnings"])


def test_budget_warning_is_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = _write_prompt(
        tmp_path,
        "classify",
        "{{contract_title}} {{clause_excerpt}}",
    )
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (
            _ClassifyOutput(contract_type=ContractType.PURCHASE),
            [],
        ),
    )
    result = classify_node(
        _state(clauses=[_clause(index) for index in range(150)]),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=library,
            max_clauses=100,
        ),
    )
    assert "条款数 150 超过上限 100，仅审核前 100 条" in result["warnings"]


def test_no_doc_processor_import() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "ai_engine" / "graphs" / "nodes.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(module.startswith("doc_processor") for module in imports)
