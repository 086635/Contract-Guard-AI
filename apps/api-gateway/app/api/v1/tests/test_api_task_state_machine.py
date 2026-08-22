"""边界族 4：任务四态状态机流转（Tasks）。

验证 Mock 映射：PENDING -> PROCESSING -> COMPLETED / FAILED，
且 percent/stage/result 随状态正确变化。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.tests.harness import KNOWN_TASK, set_task_state


def _get(client: TestClient) -> dict:
    return client.get(f"/api/v1/tasks/{KNOWN_TASK}")


class TestTaskStateMachine:
    def test_default_state_is_pending(self, client: TestClient):
        """未显式设置时，新任务默认 PENDING。"""
        body = _get(client).json()
        assert body["status"] == "PENDING"
        assert body["percent"] == 0.0
        assert body["stage"] == "queued"
        assert body["result"] is None

    def test_pending(self, client: TestClient):
        set_task_state(KNOWN_TASK, "PENDING")
        body = _get(client).json()
        assert body["status"] == "PENDING"
        assert body["percent"] == 0.0

    def test_processing(self, client: TestClient):
        set_task_state(KNOWN_TASK, "PROCESSING")
        body = _get(client).json()
        assert body["status"] == "PROCESSING"
        assert body["percent"] > 0.0
        assert body["percent"] < 1.0
        assert body["stage"] == "processing"

    def test_completed(self, client: TestClient):
        set_task_state(KNOWN_TASK, "COMPLETED")
        body = _get(client).json()
        assert body["status"] == "COMPLETED"
        assert body["percent"] == 1.0
        assert body["stage"] == "done"
        assert body["result"] is not None  # 终态携带结果

    def test_failed(self, client: TestClient):
        set_task_state(KNOWN_TASK, "FAILED")
        body = _get(client).json()
        assert body["status"] == "FAILED"
        assert body["stage"] == "failed"

    def test_progress_monotonic(self, client: TestClient):
        """完整流转 PENDING -> PROCESSING -> COMPLETED，进度单调不减。"""
        seq = ["PENDING", "PROCESSING", "COMPLETED"]
        percents = []
        for s in seq:
            set_task_state(KNOWN_TASK, s)
            percents.append(_get(client).json()["percent"])
        assert percents == sorted(percents)
        assert percents[-1] == 1.0

    def test_stream_reflects_current_state(self, client: TestClient):
        """SSE 轮询端点返回与状态机一致的进度事件。"""
        set_task_state(KNOWN_TASK, "PROCESSING")
        body = client.get(f"/api/v1/tasks/{KNOWN_TASK}/stream").json()
        assert body["events"][0]["status"] == "PROCESSING"