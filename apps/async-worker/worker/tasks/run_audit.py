"""批量大模型审核任务（§10 步骤 4-5）。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ai_engine.agents import build_rag_tool
from ai_engine.graphs import GraphDeps, build_audit_graph, new_audit_state
from ai_engine.prompts import load_default_library
from cg_observability import get_logger, log_event
from worker.adapters import to_clause_inputs
from worker.main import celery_app
from worker.progress import AuditProgressReporter

_LOG = get_logger("worker.tasks.run_audit")


def _serialize_risks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        risk.model_dump(mode="json", by_alias=True) for risk in state.get("risks", [])
    ]


def _serialize_conflicts(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        conflict.model_dump(mode="json", by_alias=True)
        for conflict in state.get("conflicts", [])
    ]


def _health_score(state: dict[str, Any]) -> dict[str, Any]:
    report = state.get("report_draft", {})
    if isinstance(report, dict) and isinstance(report.get("health_score"), dict):
        return report["health_score"]
    overall = int(state.get("health_score", 0) or 0)
    return {
        "overall": overall,
        "dimensions": {
            "compliance": overall,
            "performanceRisk": overall,
            "clauseCompleteness": overall,
        },
    }


def _payload(
    *,
    contract_id: str,
    trace_id: str,
    status: str,
    state: dict[str, Any],
    started: float,
) -> dict[str, Any]:
    warnings = list(state.get("warnings", []))
    stage_log = list(state.get("stage_log", []))
    effective_status = status
    if effective_status == "SUCCESS":
        stage_statuses = {
            str(item.get("status", "")) for item in stage_log if isinstance(item, dict)
        }
        if "FAILED" in stage_statuses:
            effective_status = "FAILED"
        elif warnings or stage_statuses - {"OK"}:
            effective_status = "DEGRADED"
    if effective_status == "FAILED":
        return {
            "contractId": contract_id,
            "traceId": trace_id,
            "status": "FAILED",
            "risks": [],
            "conflicts": [],
            "missingClauses": [],
            "healthScore": {},
            "warnings": warnings or ["audit failed"],
            "stageLog": stage_log,
            "tookMs": max(0, int((time.perf_counter() - started) * 1000)),
        }

    return {
        "contractId": contract_id,
        "traceId": trace_id,
        "status": effective_status,
        "risks": _serialize_risks(state),
        "conflicts": _serialize_conflicts(state),
        "missingClauses": list(state.get("missing_clauses", [])),
        "healthScore": _health_score(state),
        "warnings": warnings,
        "stageLog": stage_log,
        "tookMs": max(0, int((time.perf_counter() - started) * 1000)),
    }


def _failure_payload(
    *,
    contract_id: str,
    trace_id: str,
    reason: str,
    started: float,
) -> dict[str, Any]:
    return {
        "contractId": contract_id,
        "traceId": trace_id,
        "status": "FAILED",
        "risks": [],
        "conflicts": [],
        "missingClauses": [],
        "healthScore": {},
        "warnings": [reason],
        "stageLog": [],
        "tookMs": max(0, int((time.perf_counter() - started) * 1000)),
    }


@celery_app.task(name="run_audit", bind=True)
def run_audit_task(
    self,
    contract_id: str,
    clause_payload: list[dict] | None = None,
    tolerance: str = "BALANCED",
) -> dict:
    """对单个合同执行批量审核，任何异常都返回结构化结果。"""
    started = time.perf_counter()
    trace_id = uuid.uuid4().hex
    reporter = AuditProgressReporter(self, contract_id, trace_id)

    try:
        reporter.start()
        log_event(
            _LOG,
            "audit.task.start",
            trace_id=trace_id,
            contract_id=contract_id,
        )
        if clause_payload is None:
            reason = "未提供条款数据"
            result = _failure_payload(
                contract_id=contract_id,
                trace_id=trace_id,
                reason=reason,
                started=started,
            )
            result["status"] = "DEGRADED"
            reporter.finish(result)
            return result

        clauses = to_clause_inputs(clause_payload)
        if not clauses:
            reason = "无条款数据"
            result = _failure_payload(
                contract_id=contract_id,
                trace_id=trace_id,
                reason=reason,
                started=started,
            )
            result["status"] = "DEGRADED"
            reporter.finish(result)
            return result

        deps = GraphDeps(
            rag_tool=build_rag_tool(),
            llm=None,
            prompt_lib=load_default_library(),
        )
        graph = build_audit_graph(deps)
        state = new_audit_state(
            contract_id,
            clauses,
            trace_id=trace_id,
            tolerance=tolerance,
        )
        config = {"configurable": {"thread_id": contract_id}}
        final_state: dict[str, Any] = dict(state)
        stream_modes = ["updates", "values"]
        for mode, chunk in graph.stream(  # type: ignore[call-overload]
            state,
            config=config,
            stream_mode=stream_modes,
        ):
            if mode == "updates":
                for node_name in chunk:
                    reporter.stage(node_name)
                    log_event(
                        _LOG,
                        "audit.task.stage",
                        trace_id=trace_id,
                        contract_id=contract_id,
                        task_stage=node_name,
                    )
            elif mode == "values":
                final_state = chunk

        result = _payload(
            contract_id=contract_id,
            trace_id=trace_id,
            status="SUCCESS",
            state=final_state,
            started=started,
        )
        if result["status"] == "FAILED":
            warnings = [item for item in result.get("warnings", []) if item]
            reporter.fail(warnings[0] if warnings else "audit failed")
        else:
            reporter.finish(result)
        log_event(
            _LOG,
            "audit.task.done",
            trace_id=trace_id,
            contract_id=contract_id,
            status=result["status"],
        )
        return result
    except Exception as exc:  # noqa: BLE001 - 任务不得向 broker 抛异常
        reason = f"{type(exc).__name__}: {exc}"
        reporter.fail(reason)
        log_event(
            _LOG,
            "audit.task.fail",
            level="ERROR",
            trace_id=trace_id,
            contract_id=contract_id,
            error=reason,
        )
        return _failure_payload(
            contract_id=contract_id,
            trace_id=trace_id,
            reason=reason,
            started=started,
        )


__all__ = ["run_audit_task"]
