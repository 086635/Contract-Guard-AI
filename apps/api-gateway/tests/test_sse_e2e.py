"""C27 · SSE 联调验收（全离线：Fake broker / Fake dispatcher / Fake session）。

覆盖（`4.C27 §⑦`）：
1. `RUNNING` + `DONE` 两类事件都能透出；
2. `DONE` 后流转入终态、订阅被关闭；
3. 客户端断开（`aclose`）后订阅被取消；
4. 非法事件只告警、不中断流；生产代码无 `MockDataFactory`。
"""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditTask
from app.generated.schemas import Status
from app.services.context import ServiceContext
from app.services.ports import TaskStatusSnapshot
from app.services.task_service import TaskService
from tests.service_testkit import service_context

_CELERY_ID = "celery-c27"
_TASK_SERVICE_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "services" / "task_service.py"
)


class _FakeSession:
    """只覆盖 TaskRepository.get 用到的 `session.get`。"""

    def __init__(self, record: AuditTask) -> None:
        self._record = record

    async def get(self, model: Any, ident: UUID) -> AuditTask | None:
        return self._record if ident == self._record.id else None


class _FakeDispatcher:
    """固定快照的任务派发端口替身。"""

    def __init__(self, snapshot: TaskStatusSnapshot | None) -> None:
        self._snapshot = snapshot
        self.lookups: list[str] = []

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        raise NotImplementedError("SSE 用例不派发任务")

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        self.lookups.append(task_id)
        return self._snapshot


class _FakeProgressBroker:
    """记录 subscribe / unsubscribe 的进度端口替身。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self.subscribed_ids: list[str] = []
        self._unsubscribed: list[str] = []

    def subscribe(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        self.subscribed_ids.append(task_id)
        events = list(self._events)
        unsubscribed = self._unsubscribed

        async def iterator() -> AsyncIterator[dict[str, Any]]:
            try:
                for event in events:
                    yield event
            finally:
                unsubscribed.append(task_id)

        return iterator()

    def unsubscribed(self, task_id: str) -> bool:
        return task_id in self._unsubscribed


def _record() -> AuditTask:
    return AuditTask(
        id=uuid4(),
        contract_id=uuid4(),
        task_type="PARSE_DOC",
        stage="queued",
        progress=0.2,
        status=Status.running,
        celery_task_id=_CELERY_ID,
        error_message=None,
    )


def _service(
    record: AuditTask,
    dispatcher: _FakeDispatcher,
    broker: _FakeProgressBroker,
) -> TaskService:
    context: ServiceContext = service_context(
        cast(AsyncSession, _FakeSession(record)),
        task_dispatcher=dispatcher,
        progress_broker=broker,
    )
    return TaskService(context)


def _running(stage: str = "parse_text", percent: float = 0.5) -> dict[str, Any]:
    return {
        "taskId": _CELERY_ID,
        "stage": stage,
        "percent": percent,
        "status": "RUNNING",
        "result": None,
    }


def _done() -> dict[str, Any]:
    return {
        "taskId": _CELERY_ID,
        "stage": "done",
        "percent": 1.0,
        "status": "DONE",
        "result": {"ok": True},
    }


def _statuses(chunks: list[str]) -> list[str]:
    return [
        json.loads(chunk.split("data: ", 1)[1].split("\n", 1)[0])["status"]
        for chunk in chunks
    ]


def _running_snapshot() -> TaskStatusSnapshot:
    return TaskStatusSnapshot(status="RUNNING", stage="start", percent=0.0, result=None)


async def _let_finalizers_run() -> None:
    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)


def test_stream_emits_running_then_done_and_closes_subscription() -> None:
    async def scenario() -> None:
        record = _record()
        broker = _FakeProgressBroker([_running(), _done()])
        dispatcher = _FakeDispatcher(_running_snapshot())
        service = _service(record, dispatcher, broker)

        chunks = [chunk async for chunk in service.stream(record.id)]
        statuses = _statuses(chunks)

        assert "RUNNING" in statuses
        assert statuses[-1] == "DONE"
        # 修复后：Celery 查询键与进度 channel key 都应对齐 celery_task_id
        assert dispatcher.lookups == [_CELERY_ID]
        assert broker.subscribed_ids == [_CELERY_ID]

        await _let_finalizers_run()
        assert broker.unsubscribed(_CELERY_ID)

    asyncio.run(scenario())


def test_stream_with_terminal_snapshot_does_not_subscribe() -> None:
    async def scenario() -> None:
        record = _record()
        broker = _FakeProgressBroker([_running()])
        dispatcher = _FakeDispatcher(
            TaskStatusSnapshot(
                status="DONE", stage="done", percent=1.0, result={"ok": True}
            )
        )
        service = _service(record, dispatcher, broker)

        chunks = [chunk async for chunk in service.stream(record.id)]

        assert _statuses(chunks) == ["DONE"]
        assert broker.subscribed_ids == []

    asyncio.run(scenario())


def test_stream_unsubscribes_on_client_disconnect() -> None:
    async def scenario() -> None:
        record = _record()
        broker = _FakeProgressBroker([_running(), _done()])
        service = _service(record, _FakeDispatcher(_running_snapshot()), broker)

        stream = service.stream(record.id)
        await stream.__anext__()  # 当前快照
        await stream.__anext__()  # 第一条实时事件
        await stream.aclose()  # 客户端断开

        await _let_finalizers_run()
        assert broker.unsubscribed(_CELERY_ID)

    asyncio.run(scenario())


def test_stream_skips_invalid_event_and_continues() -> None:
    async def scenario() -> None:
        record = _record()
        broker = _FakeProgressBroker(
            [
                {
                    "taskId": _CELERY_ID,
                    "stage": "broken",
                },  # 缺 status/percent -> 校验失败
                _running(stage="risk", percent=0.6),
                _done(),
            ]
        )
        service = _service(record, _FakeDispatcher(_running_snapshot()), broker)

        chunks = [chunk async for chunk in service.stream(record.id)]
        statuses = _statuses(chunks)

        assert statuses[-1] == "DONE"
        assert "RUNNING" in statuses

    asyncio.run(scenario())


def test_task_service_has_no_mock_factory() -> None:
    assert "MockDataFactory" not in _TASK_SERVICE_SOURCE.read_text(encoding="utf-8")
