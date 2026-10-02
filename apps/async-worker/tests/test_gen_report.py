"""报告任务编排单测（C20 / C72：双格式产物与降级）。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from worker.progress import (
    REPORT_STAGE_PERCENT,
    REPORT_STAGES,
    ReportProgressReporter,
)
from worker.report import ReportRenderInput, render_report_docx
from worker.tasks import gen_report as gen_report_module
from worker.tasks.gen_report import gen_report_task

_WORKER_ROOT = Path(__file__).resolve().parents[1]
_DOCX_BYTES = b"PK\x03\x04docx-report"
_PDF_BYTES = b"%PDF-1.7\n%%EOF\n"


@pytest.fixture(autouse=True)
def _disable_result_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免测试时连接 Redis 结果后端。"""
    monkeypatch.setattr(
        gen_report_task.app.conf,
        "result_backend",
        None,
        raising=False,
    )


def _payload(contract_id: str = "contract-20") -> dict[str, Any]:
    return {
        "contractId": contract_id,
        "generatedAt": "2026-09-27T10:00:00+00:00",
        "risks": [],
        "conflicts": [],
        "elements": [],
    }


def _fake_render(monkeypatch: pytest.MonkeyPatch, *, data: bytes = _DOCX_BYTES) -> None:
    monkeypatch.setattr(gen_report_module, "render_report_docx", lambda payload: data)


def _fake_pdf(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: bytes | None = _PDF_BYTES,
    raises: Exception | None = None,
) -> None:
    def fake_convert(source: Path, out_dir: Path, *, timeout_s: float) -> Path:
        del source, timeout_s
        if raises is not None:
            raise raises
        out = out_dir / "report.pdf"
        out.write_bytes(data if data is not None else b"")
        return out

    monkeypatch.setattr(gen_report_module, "convert_document", fake_convert)
    monkeypatch.setattr(gen_report_module, "convert_timeout_s", lambda: 5.0)


def _fake_uploads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes, str]]:
    """替身上传：记录 (contractId, data, extension) 并返回按格式区分的 key。"""
    calls: list[tuple[str, bytes, str]] = []

    def fake_upload(
        contract_id: str,
        data: bytes,
        content_type: str | None = None,
        *,
        extension: str = "docx",
    ) -> str:
        del content_type
        calls.append((contract_id, data, extension))
        return f"reports/{contract_id}/20261002120000_abcd1234.{extension}"

    monkeypatch.setattr(gen_report_module, "upload_report", fake_upload)
    return calls


def test_task_registered_name() -> None:
    assert gen_report_task.name == "gen_report"


def test_task_signature_accepts_contract_id_and_payload() -> None:
    parameters = list(inspect.signature(gen_report_task.run).parameters)
    assert parameters[:2] == ["contract_id", "payload"]


def test_success_uploads_both_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_render(payload: ReportRenderInput) -> bytes:
        captured["render_contract_id"] = payload.contract_id
        return _DOCX_BYTES

    monkeypatch.setattr(gen_report_module, "render_report_docx", fake_render)
    _fake_pdf(monkeypatch)
    calls = _fake_uploads(monkeypatch)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result == {
        "contractId": "contract-20",
        "status": "DONE",
        "format": "DOCX",
        "storagePath": "reports/contract-20/20261002120000_abcd1234.docx",
        "pdfStoragePath": "reports/contract-20/20261002120000_abcd1234.pdf",
        "warnings": [],
    }
    assert captured["render_contract_id"] == "contract-20"
    assert calls == [
        ("contract-20", _DOCX_BYTES, "docx"),
        ("contract-20", _PDF_BYTES, "pdf"),
    ]
    # 两格式 key 必须不同（C72 §⑨-1）
    assert result["storagePath"].endswith(".docx")
    assert result["pdfStoragePath"].endswith(".pdf")
    assert result["storagePath"] != result["pdfStoragePath"]


def test_pdf_export_magic(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_render(monkeypatch)
    _fake_pdf(monkeypatch)
    calls = _fake_uploads(monkeypatch)

    gen_report_task.apply(args=("contract-20", _payload())).get()

    pdf_uploads = [data for _, data, extension in calls if extension == "pdf"]
    assert len(pdf_uploads) == 1
    assert pdf_uploads[0].startswith(b"%PDF-")


def test_real_render_docx_magic() -> None:
    """真实渲染的 DOCX 魔数为 PK 开头的 ZIP 结构（下载魔数检查的 DOCX 侧）。"""
    payload = ReportRenderInput.model_validate(_payload())
    data = render_report_docx(payload)

    assert data.startswith(b"PK\x03\x04")
    assert len(data) > 1000


def test_pdf_bytes_without_magic_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_render(monkeypatch)
    _fake_pdf(monkeypatch, data=b"not-a-pdf")
    calls = _fake_uploads(monkeypatch)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "DEGRADED"
    assert result["pdfStoragePath"] == ""
    assert result["warnings"] == ["PDF_EXPORT_FAILED:MAGIC"]
    assert [extension for _, _, extension in calls] == ["docx"]


def test_partial_report_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF 导出失败：DOCX 仍可用（DEGRADED），不返回假 PDF 路径。"""
    _fake_render(monkeypatch)
    _fake_pdf(monkeypatch, raises=RuntimeError("soffice missing"))
    calls = _fake_uploads(monkeypatch)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "DEGRADED"
    assert result["storagePath"].endswith(".docx")
    assert result["pdfStoragePath"] == ""
    assert result["warnings"] == ["PDF_EXPORT_FAILED:RuntimeError"]
    assert [extension for _, _, extension in calls] == ["docx"]
    assert json.loads(json.dumps(result)) == result


def test_pdf_upload_failure_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_render(monkeypatch)
    _fake_pdf(monkeypatch)

    def flaky_upload(
        contract_id: str,
        data: bytes,
        content_type: str | None = None,
        *,
        extension: str = "docx",
    ) -> str:
        del content_type
        if extension == "pdf":
            raise OSError("minio down")
        return f"reports/{contract_id}/20261002120000_abcd1234.{extension}"

    monkeypatch.setattr(gen_report_module, "upload_report", flaky_upload)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "DEGRADED"
    assert result["pdfStoragePath"] == ""
    assert result["warnings"] == ["PDF_UPLOAD_FAILED:OSError"]


def test_missing_payload_returns_failed() -> None:
    result = gen_report_task.apply(args=("contract-20",)).get()

    assert result["status"] == "FAILED"
    assert result["format"] == "DOCX"
    assert result["storagePath"] == ""
    assert result["pdfStoragePath"] == ""
    assert any("缺少报告输入" in item for item in result["warnings"])


def test_mismatched_contract_id_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gen_report_module,
        "render_report_docx",
        lambda payload: pytest.fail("合同 ID 不一致不得进入渲染"),
    )

    result = gen_report_task.apply(
        args=("contract-20", _payload("other-contract")),
    ).get()

    assert result["status"] == "FAILED"
    assert any("不一致" in item for item in result["warnings"])


def test_render_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_render(payload: ReportRenderInput) -> bytes:
        raise RuntimeError("render exploded")

    monkeypatch.setattr(gen_report_module, "render_report_docx", broken_render)
    monkeypatch.setattr(
        gen_report_module,
        "upload_report",
        lambda *args, **kwargs: pytest.fail("渲染失败不得继续上传"),
    )

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "FAILED"
    assert any("render exploded" in item for item in result["warnings"])


def test_docx_upload_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_render(monkeypatch)
    _fake_pdf(monkeypatch)

    def broken_upload(*args: Any, **kwargs: Any) -> str:
        raise OSError("minio down")

    monkeypatch.setattr(gen_report_module, "upload_report", broken_upload)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "FAILED"
    assert any("minio down" in item for item in result["warnings"])


def test_result_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_render(monkeypatch)
    _fake_pdf(monkeypatch)
    _fake_uploads(monkeypatch)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert json.loads(json.dumps(result)) == result


def test_report_progress_monotonic_and_unknown_ignored() -> None:
    class _Task:
        def __init__(self) -> None:
            self.events: list[tuple[str, float, str]] = []

        def update_state(
            self,
            state: str | None = None,
            meta: dict[str, Any] | None = None,
        ) -> None:
            assert meta is not None
            self.events.append((state or "", meta["percent"], meta["stage"]))

    task = _Task()
    reporter = ReportProgressReporter(task, "contract-20", "trace-20")
    reporter.start()
    reporter.stage("render")
    reporter.stage("not-a-stage")
    reporter.stage("upload")
    reporter.finish({"status": "DONE"})

    assert [stage for _, _, stage in task.events] == [
        "render",
        "render",
        "upload",
        "done",
    ]
    assert [percent for _, percent, _ in task.events] == sorted(
        percent for _, percent, _ in task.events
    )
    assert reporter.percent == 1.0


def test_report_progress_failure_does_not_raise() -> None:
    class _Broken:
        def update_state(
            self,
            state: str | None = None,
            meta: dict[str, Any] | None = None,
        ) -> None:
            raise RuntimeError("no broker")

    reporter = ReportProgressReporter(_Broken(), "contract-20", "trace-20")
    reporter.start()
    reporter.stage("render")

    assert reporter.percent == REPORT_STAGE_PERCENT["render"]


def test_report_stage_table_is_isolated() -> None:
    assert REPORT_STAGES == ("render", "upload")
    assert set(REPORT_STAGES) <= set(REPORT_STAGE_PERCENT)
    assert "extract" not in REPORT_STAGE_PERCENT


def test_no_api_gateway_import() -> None:
    source = (_WORKER_ROOT / "worker" / "tasks" / "gen_report.py").read_text(
        encoding="utf-8"
    )

    assert "from app." not in source
    assert "import app." not in source


def test_pdf_export_reuses_convert_document() -> None:
    """不新增第二套 PDF 渲染器：复用 C59 的 convert_document。"""
    source = (_WORKER_ROOT / "worker" / "tasks" / "gen_report.py").read_text(
        encoding="utf-8"
    )

    assert "from worker.tasks.convert_doc import convert_document" in source
    assert "subprocess" not in source
    assert "soffice" not in source
