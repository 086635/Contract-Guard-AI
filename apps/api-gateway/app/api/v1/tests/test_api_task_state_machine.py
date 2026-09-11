"""边界族 4：任务四态状态机流转（Tasks）。

验证 Mock 映射：PENDING -> PROCESSING -> COMPLETED / FAILED，
且 percent/stage/result 随状态正确变化。

注意：进度对象嵌在统一包络 data 内：
``{code, traceId, serverTime, data: {taskId, status, stage, percent, result}}``。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.v1.tests.harness import KNOWN_TASK, set_task_state


def _get(client: TestClient) -> dict:
    return client.get(f"/api/v1/tasks/{KNOWN_TASK}").json()


class TestTaskStateMachine:
    def test_default_state_is_pending(self, client: TestClient):
        """未显式设置时，新任务默认 PENDING。"""
        body = _get(client)
        # 包络外层（本族断言一次）
        assert body["code"] == "SUCCESS"
        assert "traceId" in body
        assert "serverTime" in body
        assert "message" not in body
        data = body["data"]
        assert data["status"] == "PENDING"
        assert data["percent"] == 0.0
        assert data["stage"] == "queued"
        assert data["result"] is None

    def test_pending(self, client: TestClient):
        set_task_state(KNOWN_TASK, "PENDING")
        data = _get(client)["data"]
        assert data["status"] == "PENDING"
        assert data["percent"] == 0.0

    def test_processing(self, client: TestClient):
        set_task_state(KNOWN_TASK, "PROCESSING")
        data = _get(client)["data"]
        assert data["status"] == "PROCESSING"
        assert data["percent"] > 0.0
        assert data["percent"] < 1.0
        assert data["stage"] == "processing"

    def test_completed(self, client: TestClient):
        set_task_state(KNOWN_TASK, "COMPLETED")
        data = _get(client)["data"]
        assert data["status"] == "COMPLETED"
        assert data["percent"] == 1.0
        assert data["stage"] == "done"
        assert data["result"] is not None  # 终态携带结果

    def test_failed(self, client: TestClient):
        set_task_state(KNOWN_TASK, "FAILED")
        data = _get(client)["data"]
        assert data["status"] == "FAILED"
        assert data["stage"] == "failed"

    def test_progress_monotonic(self, client: TestClient):
        """完整流转 PENDING -> PROCESSING -> COMPLETED，进度单调不减。"""
        seq = ["PENDING", "PROCESSING", "COMPLETED"]
        percents = []
        for s in seq:
            set_task_state(KNOWN_TASK, s)
            percents.append(_get(client)["data"]["percent"])
        assert percents == sorted(percents)
        assert percents[-1] == 1.0

    def test_stream_reflects_current_state(self, client: TestClient):
        """SSE 轮询端点返回与状态机一致的进度事件（事件内 status）。"""
        set_task_state(KNOWN_TASK, "PROCESSING")
        body = client.get(f"/api/v1/tasks/{KNOWN_TASK}/stream").json()
        assert body["events"][0]["status"] == "PROCESSING"
