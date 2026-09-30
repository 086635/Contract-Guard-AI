"""C41 rewrite Celery 任务单测。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_engine.agents import RewriteUnavailableError
from ai_engine.agents.rewriter import DiffMessage, RewriteResponse
from worker.main import celery_app
from worker.tasks import rewrite as rewrite_module
from worker.tasks.rewrite import rewrite_task


@pytest.fixture(autouse=True)
def _disable_result_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rewrite_task.app.conf, "result_backend", None, raising=False)


def _payload(request_id: str = "rewrite-r1") -> dict[str, Any]:
    return {
        "contractId": "contract-41",
        "clauseId": "3.2",
        "instruction": "改成对等解约",
        "scopedMemory": {
            "targetClauseId": "3.2",
            "recentDialog": [],
            "draftAfter": None,
        },
        "ragContext": None,
        "xRequestId": request_id,
    }


def _response(request_id: str) -> RewriteResponse:
    return RewriteResponse(
        taskId=request_id,
        clauseId="3.2",
        before="旧条款",
        after="新条款",
        diff=DiffMessage(
            type="DIFF",
            before="旧条款",
            after="新条款",
            clauseId="3.2",
        ),
        reasoningSteps=[],
        citations=[],
        updatedAt="2026-09-29T10:00:00Z",
    )


def test_task_registered() -> None:
    assert rewrite_task.name == "rewrite"
    assert "worker.tasks.rewrite" in celery_app.conf.include


def test_success_returns_rewrite_data_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_rewrite(request: Any) -> RewriteResponse:
        captured["request"] = request
        return _response(request.xRequestId)

    monkeypatch.setattr(rewrite_module, "rewrite_clause", fake_rewrite)

    result = rewrite_task.apply(args=(_payload(),)).get()

    assert captured["request"].xRequestId == "rewrite-r1"
    assert result["taskId"] == "rewrite-r1"
    assert result["clauseId"] == "3.2"
    assert result["diff"]["type"] == "DIFF"
    assert result["after"] == "新条款"


def test_invalid_payload_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        rewrite_task.apply(args=({},)).get()


def test_rewrite_exception_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_rewrite(request: Any) -> RewriteResponse:
        del request
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(rewrite_module, "rewrite_clause", broken_rewrite)

    with pytest.raises(RewriteUnavailableError, match="model unavailable") as exc_info:
        rewrite_task.apply(args=(_payload(),)).get()

    assert exc_info.value.detail["errorCode"] == "ERR_LLM_UNAVAILABLE"


def test_worker_has_no_gateway_import() -> None:
    source = Path(rewrite_module.__file__).read_text(encoding="utf-8")
    assert "from app." not in source
    assert "import app." not in source
