"""报告任务编排单测（C20）。"""

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
from worker.report import ReportRenderInput
from worker.tasks import gen_report as gen_report_module
from worker.tasks.gen_report import gen_report_task

_WORKER_ROOT = Path(__file__).resolve().parents[1]


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


def test_task_registered_name() -> None:
    assert gen_report_task.name == "gen_report"


def test_task_signature_accepts_contract_id_and_payload() -> None:
    parameters = list(inspect.signature(gen_report_task.run).parameters)
    assert parameters[:2] == ["contract_id", "payload"]


def test_success_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_render(payload: ReportRenderInput) -> bytes:
        captured["render_contract_id"] = payload.contract_id
        return b"docx-bytes"

    def fake_upload(contract_id: str, data: bytes) -> str:
        captured["upload"] = (contract_id, data)
        return "reports/contract-20/20260927100000_abcd1234.docx"

    monkeypatch.setattr(gen_report_module, "render_report_docx", fake_render)
    monkeypatch.setattr(gen_report_module, "upload_report", fake_upload)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result == {
        "contractId": "contract-20",
        "status": "DONE",
        "format": "DOCX",
        "storagePath": "reports/contract-20/20260927100000_abcd1234.docx",
        "warnings": [],
    }
    assert captured == {
        "render_contract_id": "contract-20",
        "upload": (
            "contract-20",
            b"docx-bytes",
        ),
    }


def test_missing_payload_returns_failed() -> None:
    result = gen_report_task.apply(args=("contract-20",)).get()

    assert result["status"] == "FAILED"
    assert result["format"] == "DOCX"
    assert result["storagePath"] == ""
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
        lambda contract_id, data: pytest.fail("渲染失败不得继续上传"),
    )

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "FAILED"
    assert any("render exploded" in item for item in result["warnings"])


def test_upload_failure_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gen_report_module,
        "render_report_docx",
        lambda payload: b"docx-bytes",
    )

    def broken_upload(contract_id: str, data: bytes) -> str:
        raise OSError("minio down")

    monkeypatch.setattr(gen_report_module, "upload_report", broken_upload)

    result = gen_report_task.apply(args=("contract-20", _payload())).get()

    assert result["status"] == "FAILED"
    assert any("minio down" in item for item in result["warnings"])


def test_result_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gen_report_module,
        "render_report_docx",
        lambda payload: b"docx-bytes",
    )
    monkeypatch.setattr(
        gen_report_module,
        "upload_report",
        lambda contract_id, data: "reports/contract-20/report.docx",
    )

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
