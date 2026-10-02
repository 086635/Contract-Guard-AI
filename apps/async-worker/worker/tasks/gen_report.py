"""报告生成与上传 Celery 任务（C20 / C72）。

设计要点：
1. 任务只消费网关组装的 JSON 载荷，不访问 PostgreSQL，不写 `audit_reports`。
2. 渲染 DOCX → 复用 C59 `convert_document`（LibreOffice）导 PDF → 两格式分别上传；
   不新增第二套报告渲染器。
3. 两格式都成功才写完整结果（`status=DONE`）；PDF 失败时 DOCX 仍可用，返回
   `status=DEGRADED` + `pdfStoragePath=""` + 告警，**绝不返回假 URL**（C72 §⑥）。
4. 任何异常都收敛为结构化结果，不向 broker 抛异常。

对应：`文件集合/codex/4.C20报告任务编排.md`、`4.C72报告独立Word与PDF导出.md`。
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from cg_observability import get_logger, log_event
from worker.main import celery_app
from worker.progress import ReportProgressReporter
from worker.report import ReportRenderInput, render_report_docx
from worker.storage import upload_report
from worker.tasks.convert_doc import convert_document, convert_timeout_s

_LOG = get_logger("worker.tasks.gen_report")
_FORMAT = "DOCX"
_PDF_MAGIC = b"%PDF-"


@celery_app.task(name="gen_report", bind=True)
def gen_report_task(
    self,
    contract_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """渲染 DOCX + PDF、双格式上传，并返回供网关落库的结构化结果。

    Args:
        contract_id: 合同 ID；必须与 payload 中的 `contractId` 一致。
        payload: C21 组装的报告输入，格式见 `4-3 §⑤.1`。

    Returns:
        `{contractId, status, format, storagePath, pdfStoragePath, warnings}`；
        `status ∈ {DONE, DEGRADED, FAILED}`；失败不抛给 broker。
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
        if not docx_bytes:
            raise ValueError("报告渲染结果为空")
        pdf_bytes, pdf_warning = _export_pdf(docx_bytes, trace_id=trace_id)

        reporter.stage("upload")
        storage_path = upload_report(contract_id, docx_bytes)
        pdf_storage_path = ""
        warnings: list[str] = []
        if pdf_bytes is None:
            warnings.append(pdf_warning or "PDF_EXPORT_FAILED")
        else:
            try:
                pdf_storage_path = upload_report(
                    contract_id, pdf_bytes, extension="pdf"
                )
            except Exception as exc:  # noqa: BLE001 - PDF 上传失败降级为 DEGRADED
                warnings.append(f"PDF_UPLOAD_FAILED:{type(exc).__name__}")

        status = "DONE" if pdf_storage_path else "DEGRADED"
        result = _result(
            contract_id=contract_id,
            status=status,
            storage_path=storage_path,
            pdf_storage_path=pdf_storage_path,
            warnings=warnings,
        )
        reporter.finish(result)
        log_event(
            _LOG,
            "report.task.done",
            trace_id=trace_id,
            contract_id=contract_id,
            status=status,
            storage_path=storage_path,
            pdf_storage_path=pdf_storage_path,
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


def _export_pdf(docx_bytes: bytes, *, trace_id: str) -> tuple[bytes | None, str]:
    """把 DOCX 字节转成 PDF 字节（复用 C59 `convert_document`）。

    Args:
        docx_bytes: 已渲染的 DOCX 字节。
        trace_id: 追踪 ID（仅用于日志）。

    Returns:
        `(pdf_bytes, "")` 成功；`(None, warning)` 失败——不抛异常，交由调用方降级。
    """
    work_dir = Path(tempfile.mkdtemp(prefix="cg-report-pdf-"))
    try:
        source = work_dir / "report.docx"
        source.write_bytes(docx_bytes)
        pdf = convert_document(source, work_dir, timeout_s=convert_timeout_s())
        data = pdf.read_bytes()
        if not data:
            return None, "PDF_EXPORT_FAILED:EMPTY"
        if not data.startswith(_PDF_MAGIC):
            return None, "PDF_EXPORT_FAILED:MAGIC"
        return data, ""
    except Exception as exc:  # noqa: BLE001 - 部分失败降级为 DEGRADED
        log_event(
            _LOG,
            "report.pdf.failed",
            level="WARNING",
            trace_id=trace_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None, f"PDF_EXPORT_FAILED:{type(exc).__name__}"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _result(
    *,
    contract_id: str,
    status: str,
    storage_path: str = "",
    pdf_storage_path: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """构造 JSON 可序列化的任务结果（`storagePath`=DOCX，`pdfStoragePath`=PDF）。"""
    return {
        "contractId": contract_id,
        "status": status,
        "format": _FORMAT,
        "storagePath": storage_path,
        "pdfStoragePath": pdf_storage_path,
        "warnings": list(warnings or []),
    }


__all__ = ["gen_report_task"]
