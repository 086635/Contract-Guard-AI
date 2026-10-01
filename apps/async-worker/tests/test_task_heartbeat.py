"""C63 worker 心跳上报测试（离线）。

worker 只往 Redis 打**带 TTL 的心跳键**（业务 DB 的 `last_heartbeat_at` 由网关对账回写）：
进度是事件流、心跳是存活标记，两者语义不同、不可合并。
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from cg_observability import task_heartbeat_key, task_progress_channel
from worker import progress as progress_module


class FakeRedis:
    """只记录 publish / set 的同步 Redis 替身。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.sets: list[tuple[str, str, int | None]] = []
        self._fail = fail

    def publish(self, channel: str, payload: str) -> None:
        if self._fail:
            raise RuntimeError("redis down")
        self.published.append((channel, payload))

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self._fail:
            raise RuntimeError("redis down")
        self.sets.append((key, value, ex))


def _task(task_id: str = "celery-hb-1") -> SimpleNamespace:
    return SimpleNamespace(request=SimpleNamespace(id=task_id))


@pytest.fixture(autouse=True)
def _reset_publish_state() -> None:
    """每例重置模块级降级开关（避免上一例的失败态影响下一例）。"""
    progress_module._publish_disabled = False


def test_progress_publish_also_writes_heartbeat() -> None:
    """上报进度时**同时**刷新心跳键（带 TTL）。"""
    client = FakeRedis()
    progress_module._progress_redis = lambda: client  # type: ignore[assignment]

    progress_module._publish_progress(
        _task(),
        {"taskId": "c-1", "stage": "parse_text", "percent": 0.55, "status": "RUNNING"},
        "trace-c63",
    )

    assert len(client.published) == 1
    assert client.published[0][0] == task_progress_channel("celery-hb-1")
    assert client.sets == [
        (task_heartbeat_key("celery-hb-1"), "1", progress_module.HEARTBEAT_TTL_S)
    ]


def test_heartbeat_key_matches_shared_contract() -> None:
    """心跳键名来自 cg_observability 的唯一处，且与进度 channel 不同。"""
    assert task_heartbeat_key("abc") == "task_heartbeat:abc"
    assert task_progress_channel("abc") == "task_progress:abc"
    assert task_heartbeat_key("abc") != task_progress_channel("abc")


def test_heartbeat_ttl_is_env_driven() -> None:
    """TTL 可用 `CG_TASK_HEARTBEAT_TTL_S` 覆盖（必须小于 reconciler 的 stale 阈值）。"""
    script = (
        "import os;"
        "os.environ['CG_TASK_HEARTBEAT_TTL_S']='45';"
        "from worker.progress import HEARTBEAT_TTL_S;"
        "print(HEARTBEAT_TTL_S)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "45"
    assert progress_module.HEARTBEAT_TTL_S == 120  # 默认值


def test_publish_failure_does_not_raise() -> None:
    """Redis 异常只降级告警，绝不把任务本身打挂。"""
    client = FakeRedis(fail=True)
    progress_module._progress_redis = lambda: client  # type: ignore[assignment]

    progress_module._publish_progress(_task(), {"status": "RUNNING"}, "trace-c63")

    assert progress_module._publish_disabled is True


def test_missing_redis_is_silent() -> None:
    """没有 Redis 依赖时静默跳过（不抛、不刷屏）。"""
    progress_module._progress_redis = lambda: None  # type: ignore[assignment]

    progress_module._publish_progress(_task(), {"status": "RUNNING"}, "trace-c63")

    assert progress_module._publish_disabled is True
