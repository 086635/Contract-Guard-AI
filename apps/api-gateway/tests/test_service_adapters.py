"""C8 外部依赖适配器测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.dispatch import CeleryTaskDispatcher, TaskKind
from app.services.progress import RedisProgressBroker, task_progress_channel
from app.services.storage import MinioObjectStorage


class FakeMinio:
    """MinIO SDK 测试替身。"""

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, str, timedelta]] = []
        self.get_calls: list[tuple[str, str, timedelta]] = []

    def presigned_put_object(
        self,
        bucket: str,
        file_key: str,
        *,
        expires: timedelta,
    ) -> str:
        self.put_calls.append((bucket, file_key, expires))
        return f"https://minio.local/{bucket}/{file_key}?method=PUT"

    def presigned_get_object(
        self,
        bucket: str,
        file_key: str,
        *,
        expires: timedelta,
    ) -> str:
        self.get_calls.append((bucket, file_key, expires))
        return f"https://minio.local/{bucket}/{file_key}?method=GET"

    def stat_object(self, bucket: str, file_key: str) -> SimpleNamespace:
        return SimpleNamespace(size=2048, etag='"abc123"')


class FakeCelery:
    """Celery client 测试替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object]]] = []

    def send_task(
        self,
        name: str,
        *,
        args: list[object],
        task_id: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((name, args, task_id))
        return SimpleNamespace(id=f"task-{len(self.calls)}")

    def AsyncResult(self, task_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            state="RUNNING",
            info={
                "stage": "parsing",
                "percent": 0.5,
                "result": {"taskId": task_id},
            },
        )


class FakePubSub:
    """Redis PubSub 测试替身。"""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = list(messages)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float,
    ) -> dict[str, Any] | None:
        del ignore_subscribe_messages, timeout
        if not self._messages:
            return None
        return self._messages.pop(0)

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """Redis client 测试替身。"""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.pubsub_instance = FakePubSub(messages)

    def pubsub(self) -> FakePubSub:
        return self.pubsub_instance


def test_minio_object_storage_presign_defaults_and_custom_expiry() -> None:
    client = FakeMinio()
    storage = MinioObjectStorage(client=client, bucket="contracts-test")  # type: ignore[arg-type]

    assert storage.presign_put("a.docx").endswith("method=PUT")
    assert storage.presign_get("a.docx", expires_s=120).endswith("method=GET")
    assert client.put_calls == [("contracts-test", "a.docx", timedelta(seconds=600))]
    assert client.get_calls == [("contracts-test", "a.docx", timedelta(seconds=120))]
    assert storage.stat_object("a.docx").size == 2048
    assert storage.stat_object("a.docx").etag == "abc123"

    with pytest.raises(ValueError, match="expires_s"):
        storage.presign_get("a.docx", expires_s=0)


def test_celery_dispatcher_only_sends_registered_tasks() -> None:
    client = FakeCelery()
    dispatcher = CeleryTaskDispatcher(client=client)  # type: ignore[arg-type]

    assert (
        dispatcher.send(
            TaskKind.PARSE_DOC,
            args=("key", "contract-1"),
            task_id="request-1",
        )
        == "task-1"
    )
    assert client.calls == [("parse_doc", ["key", "contract-1"], "request-1")]
    status = dispatcher.get_status("task-1")
    assert status is not None
    assert status.status == "RUNNING"
    assert status.stage == "parsing"
    assert status.percent == 0.5

    with pytest.raises(ValueError, match="未登记"):
        dispatcher.send("unknown_task")


def test_redis_progress_broker_streams_until_terminal_status() -> None:
    channel = task_progress_channel("task-1")
    client = FakeRedis(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "taskId": "task-1",
                        "stage": "parse",
                        "percent": 0.5,
                        "status": "RUNNING",
                        "result": None,
                    }
                ),
            },
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "taskId": "task-1",
                        "stage": "done",
                        "percent": 1.0,
                        "status": "DONE",
                        "result": {"ok": True},
                    }
                ),
            },
        ]
    )
    broker = RedisProgressBroker(client=client)  # type: ignore[arg-type]

    async def collect() -> list[dict[str, Any]]:
        return [event async for event in broker.subscribe("task-1")]

    events = asyncio.run(collect())

    assert [event["status"] for event in events] == ["RUNNING", "DONE"]
    assert client.pubsub_instance.subscribed == [channel]
    assert client.pubsub_instance.unsubscribed == [channel]
    assert client.pubsub_instance.closed is True


def test_adapters_are_only_modules_importing_external_sdks() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    allowed = {
        app_root / "services" / "storage.py",
        app_root / "services" / "dispatch.py",
        app_root / "services" / "progress.py",
    }
    offenders: list[str] = []
    for path in app_root.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if any(
            token in text for token in ("import minio", "import celery", "import redis")
        ):
            offenders.append(str(path.relative_to(app_root)))

    assert offenders == []
