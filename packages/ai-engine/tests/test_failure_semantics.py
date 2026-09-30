"""C54 模型失败语义与 rewrite 成功链测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ai_engine.agents.auditor import (
    PreciseCitation,
    ReasoningStep,
    RiskCategory,
    RiskLevel,
)
from ai_engine.graphs import GraphDeps, build_rewrite_graph, new_audit_state, nodes
from ai_engine.graphs import rewrite_graph as rewrite_graph_module
from ai_engine.graphs.nodes import (
    _RiskDraft,
    conflict_node,
    risk_node,
)
from ai_engine.prompts.loader import PromptLibrary


def _clause(index: int, text: str = "甲方应在 30 日内付款。") -> dict[str, Any]:
    return {
        "clause_id": f"c{index}",
        "label": f"第 {index} 条",
        "text": text,
        "level": 0,
        "parent_id": None,
        "page_no": 1,
        "bbox": None,
    }


def _risk_prompt_library(tmp_path: Path) -> PromptLibrary:
    (tmp_path / "risk.prompt").write_text(
        "{{clause_label}} {{clause_text}} {{contract_type}} "
        "{{tolerance}} {{rag_context}}",
        encoding="utf-8",
    )
    return PromptLibrary(str(tmp_path))


def _conflict_prompt_library(tmp_path: Path) -> PromptLibrary:
    (tmp_path / "conflict.prompt").write_text(
        "{{pair_block}} {{contract_type}}",
        encoding="utf-8",
    )
    return PromptLibrary(str(tmp_path))


def _rewrite_prompt_library(tmp_path: Path) -> PromptLibrary:
    (tmp_path / "rewrite.prompt").write_text(
        "{{instruction}} {{before}} {{recent_dialog}} {{rag_context}}",
        encoding="utf-8",
    )
    return PromptLibrary(str(tmp_path))


def _valid_risk_draft() -> _RiskDraft:
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


def _empty_risk_draft() -> _RiskDraft:
    return _RiskDraft(
        category=RiskCategory.PAYMENT_TERM_RISK,
        level=RiskLevel.HIGH,
        title="",
        suggestion="",
    )


def test_all_model_failure_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (None, ["model unavailable"]),
    )
    result = risk_node(
        new_audit_state(
            "C1",
            [_clause(1), _clause(2)],
            trace_id="t1",
        ),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_risk_prompt_library(tmp_path),
        ),
    )

    assert result["stage_log"][0]["status"] == "FAILED"
    assert result["risks"] == []
    assert any("ERR_LLM_ALL_CLAUSES_FAILED" in item for item in result["warnings"])


def test_partial_failure_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _invoke(*args: Any, **kwargs: Any) -> tuple[Any, list[str]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None, ["first clause unavailable"]
        return _valid_risk_draft(), []

    monkeypatch.setattr(nodes, "invoke_structured", _invoke)
    result = risk_node(
        new_audit_state(
            "C1",
            [_clause(1), _clause(2)],
            trace_id="t1",
        ),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_risk_prompt_library(tmp_path),
        ),
    )

    assert result["stage_log"][0]["status"] == "DEGRADED"
    assert len(result["risks"]) == 1
    assert any("ERR_LLM_INVALID_RISK" in item for item in result["warnings"])


def test_no_fake_empty_risks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (_empty_risk_draft(), []),
    )
    result = risk_node(
        new_audit_state("C1", [_clause(1)], trace_id="t1"),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_risk_prompt_library(tmp_path),
        ),
    )

    assert result["stage_log"][0]["status"] == "FAILED"
    assert result["risks"] == []
    assert any("空标题或空建议" in item for item in result["warnings"])


def test_all_conflict_batches_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        nodes,
        "invoke_structured",
        lambda *args, **kwargs: (None, ["conflict unavailable"]),
    )
    result = conflict_node(
        new_audit_state(
            "C1",
            [
                _clause(1, "付款 支付 价款"),
                _clause(2, "付款 支付 价款"),
            ],
            trace_id="t1",
        ),
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_conflict_prompt_library(tmp_path),
            max_conflict_pairs=50,
            conflict_batch_size=10,
        ),
    )

    assert result["stage_log"][0]["status"] == "FAILED"
    assert result["conflicts"] == []
    assert any("ERR_LLM_ALL_CONFLICTS_FAILED" in item for item in result["warnings"])


def test_rewrite_success_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = rewrite_graph_module._RewriteDraft(
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
    monkeypatch.setattr(
        rewrite_graph_module,
        "invoke_structured",
        lambda *args, **kwargs: (draft, []),
    )
    graph = build_rewrite_graph(
        GraphDeps(
            llm=object(),  # type: ignore[arg-type]
            prompt_lib=_rewrite_prompt_library(tmp_path),
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
    assert state["reasoning_steps"]
    assert state["citations"]
    assert {
        "type",
        "before",
        "after",
        "clauseId",
    } <= set(state["diff"])
