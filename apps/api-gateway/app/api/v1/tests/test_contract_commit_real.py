"""C11 合同提交与版本边界真实化测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)

from app.api.deps import get_service_context
from app.api.v1 import contracts as contracts_router
from app.db.repository.contract import (
    CommitRepository,
    ContractCreate,
    ContractRepository,
)
from app.generated.schemas import ContractStatus

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def test_commit_route_has_no_mock_import() -> None:
    path = Path(__file__).resolve().parents[1] / "contracts.py"
    text = path.read_text(encoding="utf-8")
    commit_block = text.split("async def commit_contract", 1)[1]

    assert "MockDataFactory" not in commit_block


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 合同提交集成测试",
)
def test_commit_batch_versioning_and_idempotency() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_commit(POSTGRES_DSN))


async def _exercise_commit(dsn: str) -> None:
    schema = new_schema_name("cg_c11")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract_id = uuid4()
            created = await ContractRepository(session).create(
                ContractCreate(
                    id=contract_id,
                    filename="提交测试合同.docx",
                    file_size=2048,
                    file_hash="e" * 64,
                    storage_path="contracts/commit-test.docx",
                    status=ContractStatus.pending_review,
                )
            )
            await session.commit()
            assert created.version == 1

            context = service_context(session)
            app = FastAPI()
            app.include_router(contracts_router.router, prefix="/api/v1")
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                request_id = str(uuid4())
                payload = {
                    "items": [
                        {"id": "risk-1", "accept": True},
                        {"id": "risk-2", "accept": False},
                    ],
                    "version": 1,
                }
                response = await client.post(
                    f"/api/v1/contracts/{contract_id}/commit",
                    json=payload,
                    headers={"X-Request-ID": request_id},
                )
                assert response.status_code == 200
                assert response.json()["data"] == {
                    "successIds": ["risk-1"],
                    "failedItems": [
                        {
                            "id": "risk-2",
                            "reason": "用户未采纳该修改项。",
                        }
                    ],
                }

                updated = await ContractRepository(session).get(contract_id)
                assert updated is not None
                assert updated.version == 2
                commit_rows = await CommitRepository(session).list_by_request(
                    request_id
                )
                assert len(commit_rows) == 2

                repeated = await client.post(
                    f"/api/v1/contracts/{contract_id}/commit",
                    json=payload,
                    headers={"X-Request-ID": request_id},
                )
                assert repeated.status_code == 200
                assert repeated.json()["data"] == response.json()["data"]
                assert (await ContractRepository(session).get(contract_id)).version == 2

                conflict = await client.post(
                    f"/api/v1/contracts/{contract_id}/commit",
                    json={**payload, "version": 1},
                    headers={"X-Request-ID": str(uuid4())},
                )
                assert conflict.status_code == 409

                missing = await client.post(
                    f"/api/v1/contracts/{uuid4()}/commit",
                    json={"items": [{"id": "x", "accept": True}], "version": 1},
                    headers={"X-Request-ID": str(uuid4())},
                )
                assert missing.status_code == 404

                empty = await client.post(
                    f"/api/v1/contracts/{contract_id}/commit",
                    json={"items": [], "version": 2},
                    headers={"X-Request-ID": str(uuid4())},
                )
                assert empty.status_code == 422
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
