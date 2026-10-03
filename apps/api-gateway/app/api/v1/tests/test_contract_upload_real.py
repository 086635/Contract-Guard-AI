"""C10 上传直传与解析派发真实化测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID

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
from app.api.v1 import contracts as contracts_router
from app.db.models import User
from app.db.repository.contract import ContractRepository, TaskRepository
from app.services.ports import StoredObjectInfo

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


class FakeObjectStorage:
    """对象存储测试替身。"""

    def __init__(self) -> None:
        self.put_calls: list[tuple[str, int]] = []

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        self.put_calls.append((file_key, expires_s))
        return f"https://minio.test/{file_key}?method=PUT"

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        return f"https://minio.test/{file_key}?method=GET"

    def stat_object(self, file_key: str) -> StoredObjectInfo:
        return StoredObjectInfo(size=4096, etag="etag-c10")


class FakeDispatcher:
    """任务派发测试替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def send(self, task_name: str, *, args: tuple[object, ...] = ()) -> str:
        self.calls.append((task_name, args))
        return "celery-c10-1"


def test_contracts_router_presign_upload_do_not_use_mock() -> None:
    path = Path(__file__).resolve().parents[1] / "contracts.py"
    text = path.read_text(encoding="utf-8")
    presign_block = text.split("async def presign_upload", 1)[1].split(
        "@router.get", 1
    )[0]
    upload_block = text.split("async def upload_contract", 1)[1].split(
        "@router.get", 1
    )[0]

    assert "MockDataFactory" not in presign_block
    assert "MockDataFactory" not in upload_block


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 上传链集成测试",
)
def test_upload_creates_contract_task_and_dispatches_parse() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_upload(POSTGRES_DSN))


async def _exercise_upload(dsn: str) -> None:
    schema = new_schema_name("cg_c10")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            storage = FakeObjectStorage()
            dispatcher = FakeDispatcher()
            context = service_context(
                session,
                object_storage=storage,
                task_dispatcher=dispatcher,
            )
            app = FastAPI()
            app.include_router(contracts_router.router, prefix="/api/v1")
            auth_user = install_test_auth(app, session)
            session.add(
                User(
                    id=auth_user.id,
                    username=auth_user.username,
                    password_hash="test-hash",
                    role=auth_user.role,
                    is_active=True,
                    display_name=auth_user.display_name,
                    created_at=auth_user.created_at,
                    updated_at=auth_user.updated_at,
                )
            )
            await session.flush()
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                presigned = await client.post(
                    "/api/v1/contracts/presign",
                    json={
                        "fileName": "../../采购合同.docx",
                        "fileType": "DOCX",
                    },
                )
                assert presigned.status_code == 200
                presigned_data = presigned.json()["data"]
                file_key = presigned_data["fileKey"]
                assert file_key.startswith("contracts/")
                assert "../" not in file_key
                assert file_key.endswith("采购合同.docx")
                assert presigned_data["expiresIn"] == 600

                uploaded = await client.post(
                    "/api/v1/contracts/upload",
                    json={
                        "fileKey": file_key,
                        "fileName": "采购合同.docx",
                        "fileType": "DOCX",
                    },
                )
                assert uploaded.status_code == 202
                progress = uploaded.json()["data"]
                assert progress["status"] == "RUNNING"
                assert progress["percent"] == 0.0

                contract_id = UUID(progress["result"]["contractId"])
                task_id = UUID(progress["taskId"])
                contract = await ContractRepository(session).get(contract_id)
                task = await TaskRepository(session).get(task_id)

                assert contract is not None
                assert contract.storage_path == file_key
                assert contract.file_size == 4096
                assert contract.file_hash == "etag-c10"
                assert contract.created_by_id == auth_user.id
                assert contract.legacy is False
                assert task is not None
                assert task.task_type == "PARSE_DOC"
                assert task.celery_task_id == "celery-c10-1"
                assert dispatcher.calls == [("parse_doc", (file_key, str(contract_id)))]
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
