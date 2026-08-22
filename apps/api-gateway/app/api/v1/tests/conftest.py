"""共享夹具：测试客户端 + 任务状态机清理。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1.tests.harness import build_test_app, reset_task_states


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_test_app())


@pytest.fixture(autouse=True)
def _clean_task_store():
    """每个用例前清空任务状态机，隔离状态。"""
    reset_task_states()
    yield
    reset_task_states()