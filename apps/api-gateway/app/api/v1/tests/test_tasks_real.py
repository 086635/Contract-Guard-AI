"""C14 任务查询与 SSE 真实化测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.service_testkit import (
    create_schema,
    drop_schema,
    install_test_auth,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)

from app.api.deps import get_service_context
from app.api.v1 import tasks as tasks_router
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.services.ports import TaskStatusSnapshot
from rag_contracts.enums import ContractType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


class FakeDispatcher:
    """任务状态测试替身。"""

    def __init__(self, valid_task_id: str) -> None:
        self._valid_task_id = valid_task_id

    def send(self, task_name: str, *, args: tuple[object, ...] = ()) -> str:
        return "celery-task"

    def get_status(self, task_id: str) -> TaskStatusSnapshot:
        if task_id != self._valid_task_id:
            return None
        return TaskStatusSnapshot(
            status="RUNNING",
            stage="processing",
            percent=0.6,
            result={"taskId": task_id},
        )


class FakeProgressBroker:
    """进度订阅测试替身。"""

    def subscribe(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        async def iterator() -> AsyncIterator[dict[str, Any]]:
            yield {
                "taskId": task_id,
                "stage": "processing",
                "percent": 0.8,
                "status": "RUNNING",
                "result": None,
            }
            yield {
                "taskId": task_id,
                "stage": "done",
                "percent": 1.0,
                "status": "DONE",
                "result": {"ok": True},
            }

        return iterator()


def test_tasks_router_has_no_mock_import() -> None:
    path = Path(__file__).resolve().parents[1] / "tasks.py"
    text = path.read_text(encoding="utf-8")

    assert "MockDataFactory" not in text


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 任务/SSE 集成测试",
)
def test_task_query_and_sse_stream() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_tasks(POSTGRES_DSN))


async def _exercise_tasks(dsn: str) -> None:
    schema = new_schema_name("cg_c14")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract_id = uuid4()
            await ContractRepository(session).create(
                ContractCreate(
                    id=contract_id,
                    filename="任务测试合同.docx",
                    file_size=1024,
                    file_hash="a" * 64,
                    storage_path="contracts/task-test.docx",
                    contract_type=ContractType.PURCHASE,
                )
            )
            task = await TaskRepository(session).create(
                contract_id,
                "PARSE_DOC",
                "celery-task",
            )
            await session.commit()

            # Celery 查询键应为任务行登记的 celery_task_id（TaskService 修复后）
            dispatcher = FakeDispatcher(task.celery_task_id or str(task.id))
            context = service_context(
                session,
                task_dispatcher=dispatcher,
                progress_broker=FakeProgressBroker(),
            )
            app = FastAPI()
            app.include_router(tasks_router.router, prefix="/api/v1")
            install_test_auth(app, session, persist_user=True)
            await session.commit()
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.get(f"/api/v1/tasks/{task.id}")
                assert response.status_code == 200
                assert response.json()["data"]["stage"] == "processing"
                assert response.json()["data"]["percent"] == 0.6

                issued = await client.post(f"/api/v1/tasks/{task.id}/stream-token")
                assert issued.status_code == 200
                stream = await client.get(f"/api/v1/tasks/{task.id}/stream", params={"streamToken": issued.json()["data"]["streamToken"]})
                assert stream.status_code == 200
                assert stream.headers["content-type"].startswith("text/event-stream")
                assert stream.text.count("event: progress") == 3
                assert '"status":"RUNNING"' in stream.text
                assert '"status":"DONE"' in stream.text

                missing = await client.get(f"/api/v1/tasks/{uuid4()}")
                assert missing.status_code == 404
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
