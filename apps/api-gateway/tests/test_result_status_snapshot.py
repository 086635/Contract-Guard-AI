"""解析状态复用实际任务快照，且不泄露完整解析结果。"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository.contract import TaskRecord, TaskRepository
from app.generated.schemas import Status
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.ingestion import IngestionService
from app.services.ports import ObjectStoragePort, ProgressBrokerPort, TaskStatusSnapshot
from app.services.result_service import ResultService, _audit_notice
from app.services.task_service import TaskService


class Dispatcher:
    def __init__(self, snapshot: TaskStatusSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        self.calls.append(task_id)
        return self.snapshot

    def send(self, task_name: str, *, args: tuple[object, ...] = (), task_id: str | None = None) -> str:
        raise AssertionError("查询不应新建任务")


@pytest.mark.parametrize(
    ("snapshot", "expected_status", "expected_percent", "expected_error"),
    [
        (TaskStatusSnapshot("DONE", "finished", 1.0, {"status": "DEGRADED", "text": "不得返回正文"}), Status.done, 1.0, None),
        (TaskStatusSnapshot("RUNNING", "ocr", 0.5), Status.running, 0.5, None),
        (TaskStatusSnapshot("FAILED", "failed", 0.2, {"errorMessage": "无法读取文件"}), Status.failed, 0.2, "无法读取文件"),
        (TaskStatusSnapshot("DONE", "finished", 1.0, {"status": "FAILED", "errorMessage": "解析失败", "text": "不得返回正文"}), Status.failed, 1.0, "解析失败"),
        (None, Status.running, 0.0, None),
    ],
)
def test_parse_status_uses_live_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: TaskStatusSnapshot | None,
    expected_status: Status,
    expected_percent: float,
    expected_error: str | None,
) -> None:
    import asyncio

    contract_id, task_id = uuid4(), uuid4()
    record = TaskRecord(id=task_id, contract_id=contract_id, task_type="PARSE_DOC", stage="queued", progress=0.0, status=Status.running, celery_task_id="celery-task", error_message=None)
    latest = AsyncMock(return_value=record)
    monkeypatch.setattr(TaskRepository, "get_latest_by_contract", latest)
    monkeypatch.setattr(TaskRepository, "get", AsyncMock(return_value=record))
    monkeypatch.setattr(IngestionService, "finalize_parse", AsyncMock(return_value=False))
    dispatcher = Dispatcher(snapshot)
    context = ServiceContext(cast(AsyncSession, object()), cast(ObjectStoragePort, object()), dispatcher, cast(ProgressBrokerPort, object()))
    service = ResultService(context)
    access = AsyncMock()
    monkeypatch.setattr(service, "_require_contract", access)

    progress = asyncio.run(service.parse_status(contract_id))
    assert progress is not None
    assert progress.task_id == str(task_id)
    assert progress.status is expected_status
    assert progress.percent == expected_percent
    assert progress.result == {"contractId": str(contract_id), **({"errorMessage": expected_error} if expected_error else {})}
    assert dispatcher.calls == ["celery-task"]
    access.assert_awaited_once_with(contract_id, None)
    latest.assert_awaited_once_with(contract_id, "PARSE_DOC")


def test_parse_status_preserves_missing_task_and_access_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    contract_id = uuid4()
    latest = AsyncMock(return_value=None)
    monkeypatch.setattr(TaskRepository, "get_latest_by_contract", latest)
    monkeypatch.setattr(IngestionService, "finalize_parse", AsyncMock(return_value=False))
    dispatcher = Dispatcher(None)
    context = ServiceContext(cast(AsyncSession, object()), cast(ObjectStoragePort, object()), dispatcher, cast(ProgressBrokerPort, object()))
    service = ResultService(context)
    access = AsyncMock()
    monkeypatch.setattr(service, "_require_contract", access)
    assert asyncio.run(service.parse_status(contract_id)) is None
    assert dispatcher.calls == []
    access.side_effect = ContractNotFoundError(str(contract_id))
    with pytest.raises(ContractNotFoundError):
        asyncio.run(service.parse_status(contract_id))
    assert latest.await_count == 2  # missing PARSE_DOC and SCAN_UPLOAD are both checked


@pytest.mark.parametrize(
    ("audit_status", "outcome", "score", "expected"),
    [
        (Status.done, "SUCCESS", 0, 0),
        (Status.done, "SUCCESS", 100, 100),
        (Status.done, "DEGRADED", 72, 72),
        (Status.failed, "FAILED", 100, None),
        (Status.running, "SUCCESS", 100, None),
        (Status.done, "DEGRADED", None, None),
        (Status.done, "DEGRADED", True, None),
        (Status.done, "DEGRADED", "80", None),
        (Status.done, "DEGRADED", -1, None),
        (Status.done, "DEGRADED", 101, None),
        (Status.done, "DEGRADED", float("nan"), None),
        (Status.done, "DEGRADED", float("inf"), None),
    ],
)
def test_audit_snapshot_projects_only_valid_completed_score(
    monkeypatch: pytest.MonkeyPatch,
    audit_status: Status,
    outcome: str,
    score: object,
    expected: int | None,
) -> None:
    import asyncio

    from app.generated.schemas import TaskProgress

    contract_id, parse_id, audit_id = uuid4(), uuid4(), uuid4()
    parse_record = TaskRecord(id=parse_id, contract_id=contract_id, task_type="PARSE_DOC", stage="done", progress=1, status=Status.done, celery_task_id="parse", error_message=None)
    audit_record = TaskRecord(id=audit_id, contract_id=contract_id, task_type="RUN_AUDIT", stage="risk", progress=.6, status=audit_status, celery_task_id="audit", error_message=None)
    monkeypatch.setattr(TaskRepository, "get_latest_by_contract", AsyncMock(side_effect=[parse_record, audit_record]))
    monkeypatch.setattr(IngestionService, "finalize_parse", AsyncMock(return_value=False))
    progress = [
        TaskProgress(taskId=str(parse_id), stage="done", percent=1, status=Status.done, result={"clauses": [{}], "text": "private original"}),
        TaskProgress(taskId=str(audit_id), stage="risk", percent=.6, status=audit_status, result={"status": outcome, "healthScore": {"overall": score}, "warnings": ["缺少标准依据"], "modelInfo": {"provider": "ollama", "model": "qwen3:4b-instruct", "api_key": "must-not-return"}}),
    ]
    monkeypatch.setattr(TaskService, "get", AsyncMock(side_effect=progress))
    context = ServiceContext(cast(AsyncSession, object()), cast(ObjectStoragePort, object()), Dispatcher(None), cast(ProgressBrokerPort, object()))
    service = ResultService(context)
    monkeypatch.setattr(service, "_require_contract", AsyncMock())
    actual = asyncio.run(service.parse_status(contract_id))
    assert actual is not None and actual.result is not None
    if expected is None:
        assert "auditScore" not in actual.result
    else:
        assert actual.result["auditScore"] == expected
    assert actual.result["auditModel"] == "qwen3:4b-instruct"
    assert actual.result["auditProvider"] == "ollama"
    assert actual.result["auditStage"] == "risk"
    assert actual.result["auditPercent"] == .6
    assert "must-not-return" not in str(actual.result)
    assert "private original" not in str(actual.result)


@pytest.mark.parametrize("outcome,risk_stage,expected", [
    ("DEGRADED", "OK", "标准知识库没有召回可核对依据，请人工复核。"),
    ("DEGRADED", "FAILED", "[function_calling] 模型返回空 content"),
    ("FAILED", "OK", "[function_calling] 模型返回空 content"),
])
def test_recovered_parser_notice_does_not_hide_actual_review_limitation(
    outcome: str, risk_stage: str, expected: str,
) -> None:
    payload = {
        "warnings": ["[function_calling] 模型返回空 content", "标准知识库没有召回可核对依据，请人工复核。"],
        "stageLog": [{"stage": stage, "status": risk_stage if stage == "risk" else "OK"} for stage in ("classify", "extract", "risk", "conflict")],
    }
    assert _audit_notice(payload, outcome) == expected
    assert len(payload["warnings"]) == 2  # 原诊断记录保持，不改历史任务结果。
