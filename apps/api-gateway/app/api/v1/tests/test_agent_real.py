"""C15 Agent 改写派发测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_service_context
from app.api.v1 import agent as agent_router
from app.generated.schemas import AgentRewriteRequest
from app.services.agent_service import (
    AgentRewriteTimeoutError,
    AgentService,
)
from app.services.context import ServiceContext
from app.services.ports import (
    ObjectStoragePort,
    ProgressBrokerPort,
    TaskStatusSnapshot,
)


def _rewrite_result(request_id: str, clause_id: str) -> dict[str, Any]:
    return {
        "taskId": request_id,
        "clauseId": clause_id,
        "before": "旧条款",
        "after": "新条款",
        "diff": {
            "type": "DIFF",
            "before": "旧条款",
            "after": "新条款",
            "clauseId": clause_id,
        },
        "reasoningSteps": [],
        "citations": [],
        "updatedAt": "2026-09-27T00:00:00Z",
    }


class FakeDispatcher:
    """Celery rewrite 测试替身。"""

    def __init__(
        self,
        *,
        fail_send: bool = False,
        never_done: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[object, ...], str | None]] = []
        self._fail_send = fail_send
        self._never_done = never_done
        self._status_calls = 0
        self._result: dict[str, Any] | None = None

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        if self._fail_send:
            raise RuntimeError("broker unavailable")
        self.calls.append((task_name, args, task_id))
        payload = cast(dict[str, Any], args[0])
        self._result = _rewrite_result(
            str(task_id),
            str(payload["clauseId"]),
        )
        return str(task_id)

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        del task_id
        self._status_calls += 1
        if self._never_done or self._status_calls == 1:
            return TaskStatusSnapshot(
                status="RUNNING",
                stage="rewrite",
                percent=0.5,
            )
        return TaskStatusSnapshot(
            status="DONE",
            stage="done",
            percent=1.0,
            result=self._result,
        )


class FakeBroker:
    """未使用的进度订阅替身。"""

    def subscribe(self, task_id: str):
        del task_id
        raise AssertionError("AgentService 不应订阅进度")


def _context(dispatcher: FakeDispatcher) -> ServiceContext:
    return ServiceContext(
        session=cast(AsyncSession, object()),
        object_storage=cast(ObjectStoragePort, object()),
        task_dispatcher=dispatcher,
        progress_broker=cast(ProgressBrokerPort, FakeBroker()),
    )


def _request_body() -> dict[str, Any]:
    return {
        "contractId": str(uuid4()),
        "clauseId": "3.2",
        "instruction": "改成对等解约",
        "scopedMemory": {
            "targetClauseId": "3.2",
            "recentDialog": [],
            "draftAfter": None,
        },
        "ragContext": None,
        "xRequestId": str(uuid4()),
    }


def test_agent_router_has_no_mock_import() -> None:
    path = Path(__file__).resolve().parents[1] / "agent.py"
    text = path.read_text(encoding="utf-8")

    assert "MockDataFactory" not in text


def test_agent_rewrite_dispatch_and_idempotent_cache() -> None:
    AgentService.clear_cache()
    dispatcher = FakeDispatcher()
    app = FastAPI()
    app.include_router(agent_router.router, prefix="/api/v1")
    app.dependency_overrides[get_service_context] = lambda: _context(dispatcher)
    transport = ASGITransport(app=app)
    body = _request_body()

    async def scenario() -> None:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            first = await client.post("/api/v1/agent/rewrite", json=body)
            assert first.status_code == 200
            assert first.json()["data"]["after"] == "新条款"

            second = await client.post("/api/v1/agent/rewrite", json=body)
            assert second.status_code == 200
            assert second.json()["data"] == first.json()["data"]

    asyncio.run(scenario())
    assert len(dispatcher.calls) == 1
    task_name, args, task_id = dispatcher.calls[0]
    assert task_name == "rewrite"
    assert args[0]["xRequestId"] == body["xRequestId"]
    assert task_id == body["xRequestId"]


def test_agent_rewrite_broker_failure_returns_502() -> None:
    AgentService.clear_cache()
    dispatcher = FakeDispatcher(fail_send=True)
    app = FastAPI()
    app.include_router(agent_router.router, prefix="/api/v1")
    app.dependency_overrides[get_service_context] = lambda: _context(dispatcher)

    async def scenario() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post("/api/v1/agent/rewrite", json=_request_body())
            assert response.status_code == 502

    asyncio.run(scenario())


def test_agent_rewrite_timeout_is_explicit() -> None:
    AgentService.clear_cache()
    dispatcher = FakeDispatcher(never_done=True)
    body = AgentRewriteRequest.model_validate(_request_body())
    service = AgentService(
        _context(dispatcher),
        timeout_s=0.01,
        poll_interval_s=0.001,
    )

    with pytest.raises(AgentRewriteTimeoutError):
        asyncio.run(service.rewrite(body))
