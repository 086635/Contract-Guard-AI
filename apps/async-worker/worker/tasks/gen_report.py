"""报告生成与上传 Celery 任务（C20 / `4-3 §⑤.1`）。

设计要点：
1. 任务只消费网关组装的 JSON 载荷，不访问 PostgreSQL，不写 `audit_reports`。
2. 渲染与上传是两个独立阶段，任何异常都收敛为 `FAILED` 结果。
3. 返回结果只含 JSON 基本类型，由 C21 在网关侧按 `storagePath` 落库。

对应：`文件集合/codex/4.C20报告任务编排.md`。
"""

from __future__ import annotations

import uuid
from typing import Any

from cg_observability import get_logger, log_event
from worker.main import celery_app
from worker.progress import ReportProgressReporter
from worker.report import ReportRenderInput, render_report_docx
from worker.storage import upload_report

_LOG = get_logger("worker.tasks.gen_report")
_FORMAT = "DOCX"


@celery_app.task(name="gen_report", bind=True)
def gen_report_task(
    self,
    contract_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """渲染 DOCX、上传 MinIO，并返回供网关落库的结构化结果。

    Args:
        contract_id: 合同 ID；必须与 payload 中的 `contractId` 一致。
        payload: C21 组装的报告输入，格式见 `4-3 §⑤.1`。

    Returns:
        `{contractId, status, format, storagePath, warnings}`；失败不抛给 broker。
    """
    trace_id = uuid.uuid4().hex
    reporter = ReportProgressReporter(self, contract_id, trace_id)
    reporter.start()
    log_event(
        _LOG,
        "report.task.start",
        trace_id=trace_id,
        contract_id=contract_id,
    )

    try:
        if payload is None:
            raise ValueError("缺少报告输入")

        render_input = ReportRenderInput.model_validate(payload)
        if render_input.contract_id != contract_id:
            raise ValueError("payload contractId 与任务参数不一致")

        reporter.stage("render")
        docx_bytes = render_report_docx(render_input)

        reporter.stage("upload")
        storage_path = upload_report(contract_id, docx_bytes)

        result = _result(
            contract_id=contract_id,
            status="DONE",
            storage_path=storage_path,
        )
        reporter.finish(result)
        log_event(
            _LOG,
            "report.task.done",
            trace_id=trace_id,
            contract_id=contract_id,
            storage_path=storage_path,
        )
        return result
    except Exception as exc:  # noqa: BLE001 - 任务不得向 broker 抛异常
        reason = f"{type(exc).__name__}: {exc}"
        reporter.fail(reason)
        log_event(
            _LOG,
            "report.task.fail",
            level="ERROR",
            trace_id=trace_id,
            contract_id=contract_id,
            error=reason,
        )
        return _result(
            contract_id=contract_id,
            status="FAILED",
            warnings=[reason],
        )


def _result(
    *,
    contract_id: str,
    status: str,
    storage_path: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """构造 JSON 可序列化的任务结果。"""
    return {
        "contractId": contract_id,
        "status": status,
        "format": _FORMAT,
        "storagePath": storage_path,
        "warnings": list(warnings or []),
    }


__all__ = ["gen_report_task"]
