"""C7 Service 基座与依赖注入测试。"""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_db,
    get_object_storage,
    get_progress_broker,
    get_service_context,
    get_task_dispatcher,
)
from app.services.context import ServiceContext


class FakeObjectStorage:
    """对象存储测试替身。"""

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        return f"put://{file_key}?expires={expires_s}"

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        return f"get://{file_key}?expires={expires_s}"


class FakeTaskDispatcher:
    """任务派发测试替身。"""

    def send(self, task_name: str, *, args: tuple[object, ...] = ()) -> str:
        return f"{task_name}:{len(args)}"


class FakeProgressBroker:
    """进度订阅测试替身。"""

    def subscribe(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        async def iterator() -> AsyncIterator[dict[str, Any]]:
            yield {"taskId": task_id, "status": "RUNNING"}

        return iterator()


def _fake_context() -> ServiceContext:
    return ServiceContext(
        session=cast(AsyncSession, object()),
        object_storage=FakeObjectStorage(),
        task_dispatcher=FakeTaskDispatcher(),
        progress_broker=FakeProgressBroker(),
    )


def test_service_context_is_frozen_and_builds() -> None:
    context = _fake_context()

    assert isinstance(context.object_storage, FakeObjectStorage)
    assert context.task_dispatcher.send("probe") == "probe:0"
    with pytest.raises(FrozenInstanceError):
        context.session = cast(AsyncSession, object())  # type: ignore[misc]


def test_dependency_overrides_build_service_context() -> None:
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: cast(AsyncSession, object())
    app.dependency_overrides[get_object_storage] = FakeObjectStorage
    app.dependency_overrides[get_task_dispatcher] = FakeTaskDispatcher
    app.dependency_overrides[get_progress_broker] = FakeProgressBroker

    @app.get("/probe")
    async def probe(
        context: ServiceContext = Depends(get_service_context),
    ) -> dict[str, str]:
        assert isinstance(context.object_storage, FakeObjectStorage)
        return {"storage": type(context.object_storage).__name__}

    response = TestClient(app).get("/probe")
    assert response.status_code == 200
    assert response.json() == {"storage": "FakeObjectStorage"}


def test_ports_do_not_import_concrete_sdks() -> None:
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "ports.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert (
        not {
            "minio",
            "celery",
            "redis",
        }
        & imported_modules
    )
