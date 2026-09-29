"""C9/C38 反馈真实化测试。"""

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
from app.api.v1 import feedback as feedback_router
from app.db.repository.config_feedback import FeedbackRepository
from app.db.repository.contract import ContractCreate, ContractRepository
from app.generated.schemas import TargetType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def test_feedback_router_has_no_mock_import() -> None:
    path = Path(__file__).resolve().parents[1] / "feedback.py"
    text = path.read_text(encoding="utf-8")

    assert "MockDataFactory" not in text


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 反馈服务集成测试",
)
def test_feedback_route_persists_and_lists_feedback() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_feedback_route(POSTGRES_DSN))


async def _exercise_feedback_route(dsn: str) -> None:
    schema = new_schema_name("cg_c38_feedback")
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
                    filename="feedback-contract.docx",
                    file_size=256,
                    file_hash=uuid4().hex,
                    storage_path="feedback-contract.docx",
                )
            )
            await session.commit()

            context = service_context(session)
            app = FastAPI()
            app.include_router(feedback_router.router, prefix="/api/v1")
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                first = await client.post(
                    "/api/v1/feedback",
                    json={
                        "contractId": str(contract_id),
                        "targetType": "RISK",
                        "targetId": "risk-c38-1",
                        "verdict": "CORRECT",
                        "userCorrection": "风险判断正确。",
                        "reviewer": "法务主管",
                    },
                )
                assert first.status_code == 201
                assert first.json()["data"]["reviewer"] == "法务主管"

                second = await client.post(
                    "/api/v1/feedback",
                    json={
                        "contractId": str(contract_id),
                        "targetType": "SEAL",
                        "targetId": "seal-c38-1",
                        "verdict": "FALSE_POSITIVE",
                    },
                )
                third = await client.post(
                    "/api/v1/feedback",
                    json={
                        "contractId": str(contract_id),
                        "targetType": "RISK",
                        "targetId": "risk-c38-2",
                        "verdict": "MISSING",
                    },
                )
                assert second.status_code == 201
                assert third.status_code == 201

                listed = await client.get(
                    "/api/v1/feedback",
                    params={"contractId": str(contract_id)},
                )
                assert listed.status_code == 200
                body = listed.json()["data"]
                assert body["total"] == 3
                assert body["page"] == 1
                assert body["pageSize"] == 20
                assert body["hasMore"] is False
                assert {item["targetId"] for item in body["items"]} == {
                    "risk-c38-1",
                    "risk-c38-2",
                    "seal-c38-1",
                }

                seal_only = await client.get(
                    "/api/v1/feedback",
                    params={"targetType": "SEAL"},
                )
                assert seal_only.json()["data"]["total"] == 1
                missing_only = await client.get(
                    "/api/v1/feedback",
                    params={"verdict": "MISSING"},
                )
                assert missing_only.json()["data"]["total"] == 1

                paged = await client.get(
                    "/api/v1/feedback",
                    params={"contractId": str(contract_id), "page": 2, "pageSize": 2},
                )
                assert paged.status_code == 200
                assert paged.json()["data"]["total"] == 3
                assert len(paged.json()["data"]["items"]) == 1
                assert paged.json()["data"]["hasMore"] is False

                invalid_page = await client.get(
                    "/api/v1/feedback",
                    params={"pageSize": 101},
                )
                assert invalid_page.status_code == 422

                records, total = await FeedbackRepository(session).list(
                    TargetType.risk,
                    contract_id=contract_id,
                )
                assert len(records) == 2 and total == 2
                assert all(record.contract_id == contract_id for record in records)
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
