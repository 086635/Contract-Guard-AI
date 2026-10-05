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

    def __init__(self, name: str = "internal") -> None:
        self.name = name
        self.put_calls: list[tuple[str, str, timedelta]] = []
        self.get_calls: list[tuple[str, str, timedelta]] = []
        self.stat_calls: list[tuple[str, str]] = []

    def presigned_put_object(
        self,
        bucket: str,
        file_key: str,
        *,
        expires: timedelta,
    ) -> str:
        self.put_calls.append((bucket, file_key, expires))
        return f"https://{self.name}/{bucket}/{file_key}?method=PUT"

    def presigned_get_object(
        self,
        bucket: str,
        file_key: str,
        *,
        expires: timedelta,
    ) -> str:
        self.get_calls.append((bucket, file_key, expires))
        return f"https://{self.name}/{bucket}/{file_key}?method=GET"

    def stat_object(self, bucket: str, file_key: str) -> SimpleNamespace:
        self.stat_calls.append((bucket, file_key))
        return SimpleNamespace(size=2048, etag='"abc123"')


class FakeCelery:
    """Celery client 测试替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[object], str | None, str | None]] = []

    def send_task(
        self,
        name: str,
        *,
        args: list[object],
        task_id: str | None = None,
        queue: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append((name, args, task_id, queue))
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


def test_minio_object_storage_separates_public_and_internal_clients() -> None:
    internal = FakeMinio("internal")
    public = FakeMinio("public")
    storage = MinioObjectStorage(  # type: ignore[arg-type]
        client=internal,
        public_client=public,
        bucket="contracts-test",
    )

    assert storage.presign_put("a.docx").startswith("https://public/")
    assert storage.presign_get("a.docx", expires_s=120).startswith("https://public/")
    assert public.put_calls == [
        ("contracts-test", "a.docx", timedelta(seconds=600))
    ]
    assert public.get_calls == [
        ("contracts-test", "a.docx", timedelta(seconds=120))
    ]
    assert internal.put_calls == []
    assert internal.get_calls == []

    assert storage.stat_object("a.docx").size == 2048
    assert storage.stat_object("a.docx").etag == "abc123"
    assert internal.stat_calls == [
        ("contracts-test", "a.docx"),
        ("contracts-test", "a.docx"),
    ]
    assert public.stat_calls == []

    with pytest.raises(ValueError, match="expires_s"):
        storage.presign_get("a.docx", expires_s=0)


def test_minio_public_endpoint_falls_back_to_internal_endpoint() -> None:
    from app.config import Settings

    fallback = Settings(
        _env_file=None,
        minio_endpoint="minio-internal:9000",
        minio_public_endpoint=None,
    )
    explicit = Settings(
        _env_file=None,
        minio_endpoint="minio-internal:9000",
        minio_public_endpoint="minio-public:9000",
    )

    assert fallback.resolved_minio_public_endpoint == "minio-internal:9000"
    assert explicit.resolved_minio_public_endpoint == "minio-public:9000"


def test_minio_storage_builds_internal_and_public_clients_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import storage as storage_module

    endpoints: list[str] = []
    regions: list[str] = []

    class RecordingMinio:
        def __init__(self, endpoint: str, **kwargs: object) -> None:
            endpoints.append(endpoint)
            regions.append(str(kwargs["region"]))

    monkeypatch.setattr(storage_module, "Minio", RecordingMinio)
    monkeypatch.setattr(storage_module.settings, "minio_endpoint", "minio:9000")
    monkeypatch.setattr(
        storage_module.settings,
        "minio_public_endpoint",
        "localhost:9000",
    )

    storage_module.MinioObjectStorage()

    assert endpoints == ["minio:9000", "localhost:9000"]
    assert regions == ["us-east-1", "us-east-1"]


def test_minio_storage_uses_independent_public_tls_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import storage as storage_module

    secure_values: list[bool] = []

    class RecordingMinio:
        def __init__(self, endpoint: str, **kwargs: object) -> None:
            secure_values.append(bool(kwargs["secure"]))

    monkeypatch.setattr(storage_module, "Minio", RecordingMinio)
    monkeypatch.setattr(storage_module.settings, "minio_endpoint", "minio:9000")
    monkeypatch.setattr(
        storage_module.settings,
        "minio_public_endpoint",
        "127.0.0.1:9005",
    )
    monkeypatch.setattr(storage_module.settings, "minio_secure", False)
    monkeypatch.setattr(storage_module.settings, "minio_public_secure", True)

    storage_module.MinioObjectStorage()

    assert secure_values == [False, True]


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
    assert client.calls == [("parse_doc", ["key", "contract-1"], "request-1", None)]
    status = dispatcher.get_status("task-1")
    assert status is not None
    assert status.status == "RUNNING"
    assert status.stage == "parsing"
    assert status.percent == 0.5

    assert (
        dispatcher.send(TaskKind.CONVERT_DOC, args=("contract-1", "source.docx"))
        == "task-2"
    )
    assert client.calls[-1] == (
        "convert_doc",
        ["contract-1", "source.docx"],
        None,
        "convert",
    )

    with pytest.raises(ValueError, match="未登记"):
        dispatcher.send("unknown_task")


@pytest.mark.parametrize(
    ("state", "info", "expected_stage", "expected_percent"),
    [
        ("SUCCESS", {"status": "DEGRADED"}, "finished", 1.0),
        ("STARTED", {}, "processing", 0.0),
        ("STARTED", {"stage": "ocr", "percent": 0.5}, "ocr", 0.5),
    ],
)
def test_celery_snapshot_defaults_follow_task_state(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    info: dict[str, object],
    expected_stage: str,
    expected_percent: float,
) -> None:
    client = FakeCelery()
    monkeypatch.setattr(client, "AsyncResult", lambda task_id: SimpleNamespace(state=state, info=info))
    snapshot = CeleryTaskDispatcher(client=client).get_status("task-1")  # type: ignore[arg-type]
    assert snapshot is not None
    assert snapshot.stage == expected_stage
    assert snapshot.percent == expected_percent


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
