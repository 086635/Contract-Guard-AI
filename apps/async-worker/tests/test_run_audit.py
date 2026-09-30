"""审核任务接线单测（T-B8 / C10）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_engine.agents.auditor import (
    EnhancedRiskItem,
    RiskCategory,
    RiskLevel,
)
from ai_engine.graphs import AUDIT_STAGES
from worker.progress import AUDIT_STAGE_PERCENT, AuditProgressReporter
from worker.tasks import run_audit as run_audit_module
from worker.tasks.run_audit import run_audit_task

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _disable_result_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_audit_task.app.conf, "result_backend", None, raising=False)


def _clause(index: int = 1) -> dict[str, Any]:
    return {
        "clause_id": f"c{index}",
        "label": f"第 {index} 条",
        "text": "甲方应在 30 日内付款。",
        "level": 0,
        "page_no": 1,
        "bbox": None,
    }


def _risk(index: int) -> EnhancedRiskItem:
    return EnhancedRiskItem(
        id=f"risk-{index}",
        clauseId=f"c{index}",
        category=RiskCategory.PAYMENT_TERM_RISK,
        level=RiskLevel.HIGH,
        title="付款风险",
        originalText="甲方应在 30 日内付款。",
        bbox={},
        suggestion="明确付款节点。",
    )


class _FakeGraph:
    def __init__(
        self,
        *,
        fail: bool = False,
        captured: dict[str, Any] | None = None,
        clause_count: int = 1,
        stage_statuses: dict[str, str] | None = None,
        risks: list[EnhancedRiskItem] | None = None,
    ) -> None:
        self.fail = fail
        self.captured = captured if captured is not None else {}
        self.clause_count = clause_count
        self.stage_statuses = stage_statuses or {}
        self.risks = risks

    def stream(
        self,
        state: dict[str, Any],
        *,
        config: dict[str, Any],
        stream_mode: list[str],
    ) -> Any:
        self.captured["config"] = config
        self.captured["stream_mode"] = stream_mode
        if self.fail:
            raise RuntimeError("graph exploded")

        cumulative = dict(state)
        cumulative["stage_log"] = []
        cumulative["warnings"] = []
        cumulative["risks"] = (
            list(self.risks)
            if self.risks is not None
            else [_risk(index) for index in range(1, self.clause_count + 1)]
        )
        cumulative["health_score"] = 90
        cumulative["report_draft"] = {
            "health_score": {
                "overall": 90,
                "dimensions": {
                    "compliance": 90,
                    "performanceRisk": 90,
                    "clauseCompleteness": 100,
                },
            }
        }
        for stage in AUDIT_STAGES:
            cumulative["stage_log"] = [
                *cumulative["stage_log"],
                {
                    "stage": stage,
                    "status": self.stage_statuses.get(stage, "OK"),
                    "latency_ms": 0,
                    "detail": "ok",
                },
            ]
            yield "updates", {stage: {}}
            yield "values", dict(cumulative)


def _patch_success_graph(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clause_count: int = 1,
    stage_statuses: dict[str, str] | None = None,
    risks: list[EnhancedRiskItem] | None = None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        run_audit_module,
        "build_rag_tool",
        lambda: object(),
    )
    monkeypatch.setattr(
        run_audit_module,
        "load_default_library",
        lambda: object(),
    )
    monkeypatch.setattr(
        run_audit_module,
        "build_audit_graph",
        lambda deps: _FakeGraph(
            captured=captured,
            clause_count=clause_count,
            stage_statuses=stage_statuses,
            risks=risks,
        ),
    )
    return captured


def test_task_registered_name() -> None:
    assert run_audit_task.name == "run_audit"


def test_run_audit_success_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_success_graph(monkeypatch, clause_count=2)
    result = run_audit_task.apply(args=("C1", [_clause(1), _clause(2)])).get()
    assert result["status"] == "SUCCESS"
    assert result["traceId"]
    assert len(result["risks"]) == 2
    assert result["healthScore"]["overall"] == 90


def test_failed_stage_maps_task_to_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_success_graph(
        monkeypatch,
        risks=[],
        stage_statuses={"risk": "FAILED"},
    )

    result = run_audit_task.apply(args=("C1", [_clause()])).get()

    assert result["status"] == "FAILED"
    assert result["risks"] == []
    assert result["conflicts"] == []
    assert result["missingClauses"] == []
    assert result["healthScore"] == {}


def test_degraded_stage_maps_task_to_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_success_graph(
        monkeypatch,
        stage_statuses={"risk": "DEGRADED"},
    )

    result = run_audit_task.apply(args=("C1", [_clause()])).get()

    assert result["status"] == "DEGRADED"


def test_empty_payload_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_audit_module,
        "build_audit_graph",
        lambda deps: pytest.fail("空条款不得构图"),
    )
    result = run_audit_task.apply(args=("C1", [])).get()
    assert result["status"] == "DEGRADED"
    assert any("无条款数据" in item for item in result["warnings"])


def test_progress_sequence_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, float]] = []

    class _Reporter:
        def __init__(self, task: Any, contract_id: str, trace_id: str) -> None:
            self.percent = 0.0

        def start(self) -> None:
            events.append(("start", 0.0))

        def stage(self, name: str) -> None:
            self.percent = AUDIT_STAGE_PERCENT[name]
            events.append((name, self.percent))

        def finish(self, result: dict | None = None) -> None:
            events.append(("done", 1.0))

        def fail(self, reason: str) -> None:
            events.append(("failed", self.percent))

    monkeypatch.setattr(run_audit_module, "AuditProgressReporter", _Reporter)
    _patch_success_graph(monkeypatch)
    run_audit_task.apply(args=("C1", [_clause()])).get()
    assert [name for name, _ in events] == ["start", *AUDIT_STAGES, "done"]
    assert [percent for _, percent in events] == sorted(
        percent for _, percent in events
    )


def test_thread_id_is_contract_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_success_graph(monkeypatch)
    run_audit_task.apply(args=("contract-42", [_clause()])).get()
    assert captured["config"]["configurable"]["thread_id"] == "contract-42"
    assert captured["stream_mode"] == ["updates", "values"]


def test_graph_failure_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        run_audit_module,
        "build_rag_tool",
        lambda: object(),
    )
    monkeypatch.setattr(
        run_audit_module,
        "load_default_library",
        lambda: object(),
    )
    monkeypatch.setattr(
        run_audit_module,
        "build_audit_graph",
        lambda deps: _FakeGraph(fail=True),
    )
    result = run_audit_task.apply(args=("C1", [_clause()])).get()
    assert result["status"] == "FAILED"
    assert any("graph exploded" in item for item in result["warnings"])


def test_result_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_success_graph(monkeypatch)
    result = run_audit_task.apply(args=("C1", [_clause()])).get()
    assert json.loads(json.dumps(result)) == result


def test_progress_unknown_stage_is_ignored() -> None:
    class _Task:
        def __init__(self) -> None:
            self.events: list[str] = []

        def update_state(
            self, state: str | None = None, meta: dict | None = None
        ) -> None:
            assert meta is not None
            self.events.append(str(meta["stage"]))

    task = _Task()
    reporter = AuditProgressReporter(task, "C1", "t1")
    reporter.stage("not-a-stage")
    assert task.events == []
    assert reporter.percent == 0.0


def test_no_api_gateway_import() -> None:
    for path in (
        _REPO_ROOT / "apps" / "async-worker" / "worker" / "adapters.py",
        _REPO_ROOT / "apps" / "async-worker" / "worker" / "tasks" / "run_audit.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "from app." not in source
        assert "import app." not in source
