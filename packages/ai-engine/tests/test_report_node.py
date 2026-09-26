"""报告草稿节点单测（T-B3 / C8）。"""

from __future__ import annotations

import json

from ai_engine.agents.auditor import EnhancedRiskItem, RiskCategory, RiskLevel
from ai_engine.agents.conflict import ConflictType, CrossClauseConflict
from ai_engine.graphs import GraphDeps, new_audit_state
from ai_engine.graphs.nodes import (
    build_report_draft,
    compute_health_score,
    report_draft_node,
)


def _risk(
    risk_id: str,
    *,
    level: RiskLevel = RiskLevel.LOW,
    category: RiskCategory = RiskCategory.OTHER,
) -> EnhancedRiskItem:
    return EnhancedRiskItem(
        id=risk_id,
        clauseId="c1",
        category=category,
        level=level,
        title="风险",
        originalText="原文",
        bbox={},
        suggestion="建议",
    )


def _conflict(
    conflict_id: str,
    *,
    conflict_type: ConflictType,
    level: RiskLevel = RiskLevel.LOW,
) -> CrossClauseConflict:
    return CrossClauseConflict(
        id=conflict_id,
        sourceClauseId="c1",
        targetClauseId="c2",
        conflictType=conflict_type,
        description="矛盾",
        level=level,
        suggestion="建议",
    )


def test_empty_is_100() -> None:
    score = compute_health_score([], [], [])
    assert score == {
        "overall": 100,
        "dimensions": {
            "compliance": 100,
            "performanceRisk": 100,
            "clauseCompleteness": 100,
        },
    }


def test_formula_performance_high_compliance_medium() -> None:
    score = compute_health_score(
        [
            _risk(
                "r1",
                level=RiskLevel.HIGH,
                category=RiskCategory.PENALTY_TOO_HIGH,
            ),
            _risk(
                "r2",
                level=RiskLevel.MEDIUM,
                category=RiskCategory.OTHER,
            ),
        ],
        [],
        [{"id": "missing-1"}],
    )
    assert score["dimensions"] == {
        "compliance": 95,
        "performanceRisk": 90,
        "clauseCompleteness": 97,
    }
    assert score["overall"] == 93


def test_clamp_zero() -> None:
    risks = [
        *(
            _risk(
                f"p{index}",
                level=RiskLevel.HIGH,
                category=RiskCategory.PENALTY_TOO_HIGH,
            )
            for index in range(11)
        ),
        *(
            _risk(
                f"c{index}",
                level=RiskLevel.HIGH,
                category=RiskCategory.OTHER,
            )
            for index in range(11)
        ),
    ]
    score = compute_health_score(
        risks,
        [],
        [{"id": str(index)} for index in range(40)],
    )
    assert score["dimensions"] == {
        "compliance": 0,
        "performanceRisk": 0,
        "clauseCompleteness": 0,
    }
    assert score["overall"] == 0


def test_conflict_dimension_mapping() -> None:
    score = compute_health_score(
        [],
        [
            _conflict(
                "c1",
                conflict_type=ConflictType.PAYMENT_CONTRADICTION,
            ),
            _conflict(
                "c2",
                conflict_type=ConflictType.LIABILITY_CONTRADICTION,
            ),
            _conflict(
                "c3",
                conflict_type=ConflictType.TERM_CONTRADICTION,
            ),
            _conflict(
                "c4",
                conflict_type=ConflictType.OTHER,
            ),
        ],
        [],
    )
    assert score["dimensions"] == {
        "compliance": 98,
        "performanceRisk": 98,
        "clauseCompleteness": 100,
    }


def test_risk_category_mapping_is_complete() -> None:
    performance_categories = {
        RiskCategory.UNLIMITED_LIABILITY,
        RiskCategory.LIABILITY_IMBALANCE,
        RiskCategory.PENALTY_TOO_HIGH,
        RiskCategory.PAYMENT_TERM_RISK,
        RiskCategory.UNILATERAL_TERMINATION,
    }
    compliance_categories = {
        RiskCategory.UNFAVORABLE_JURISDICTION,
        RiskCategory.MISSING_DISPUTE_CLAUSE,
        RiskCategory.FORCE_MAJEURE_ABSENT,
        RiskCategory.IP_OWNERSHIP_RISK,
        RiskCategory.VAGUE_CONFIDENTIALITY,
        RiskCategory.OTHER,
    }
    assert performance_categories | compliance_categories == set(RiskCategory)
    assert not performance_categories & compliance_categories

    score = compute_health_score(
        [
            _risk(
                category.value,
                category=category,
            )
            for category in RiskCategory
        ],
        [],
        [],
    )
    assert score["dimensions"]["performanceRisk"] == 90
    assert score["dimensions"]["compliance"] == 88


def test_top_risks_sorted_and_capped() -> None:
    risks = [
        _risk("h2", level=RiskLevel.HIGH),
        _risk("l1", level=RiskLevel.LOW),
        _risk("h1", level=RiskLevel.HIGH),
        _risk("m2", level=RiskLevel.MEDIUM),
        _risk("m1", level=RiskLevel.MEDIUM),
        _risk("l2", level=RiskLevel.LOW),
    ]
    score = compute_health_score(risks, [], [])
    report = build_report_draft(risks, [], [], score)
    assert [item["id"] for item in report["top_risks"]] == [
        "h1",
        "h2",
        "m1",
        "m2",
        "l1",
    ]


def test_risk_counts_always_have_three_levels() -> None:
    risks = [_risk("h1", level=RiskLevel.HIGH)]
    score = compute_health_score(risks, [], [])
    report = build_report_draft(risks, [], [], score)
    assert report["risk_counts"] == {
        "HIGH": 1,
        "MEDIUM": 0,
        "LOW": 0,
    }


def test_report_is_json_serializable() -> None:
    risks = [_risk("h1", level=RiskLevel.HIGH)]
    conflicts = [
        _conflict(
            "c1",
            conflict_type=ConflictType.PAYMENT_CONTRADICTION,
        )
    ]
    missing = [{"id": "m1"}]
    score = compute_health_score(risks, conflicts, missing)
    report = build_report_draft(risks, conflicts, missing, score)
    assert json.loads(json.dumps(report)) == report


def test_report_node_has_no_model_or_rag() -> None:
    state = new_audit_state("C1", [], trace_id="t1")
    state["risks"] = [_risk("h1", level=RiskLevel.HIGH)]
    state["conflicts"] = [
        _conflict(
            "c1",
            conflict_type=ConflictType.PAYMENT_CONTRADICTION,
        )
    ]
    state["missing_clauses"] = [{"id": "m1"}]

    result = report_draft_node(state, GraphDeps())
    assert "llm_calls" not in result
    assert "rag_calls" not in result
    assert result["report_draft"]["health_score"]["overall"] == result["health_score"]
    assert result["stage_log"][0]["status"] == "OK"


def test_summary_is_deterministic() -> None:
    risks = [_risk("h1", level=RiskLevel.HIGH)]
    conflicts = [
        _conflict(
            "c1",
            conflict_type=ConflictType.PAYMENT_CONTRADICTION,
        )
    ]
    missing = [{"id": "m1"}]
    score = compute_health_score(risks, conflicts, missing)
    first = build_report_draft(risks, conflicts, missing, score)
    second = build_report_draft(risks, conflicts, missing, score)
    assert first["summary"] == second["summary"]
    assert first == second
