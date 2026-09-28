"""C9 反馈真实化测试。"""

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
def test_feedback_route_persists_feedback() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_feedback_route(POSTGRES_DSN))


async def _exercise_feedback_route(dsn: str) -> None:
    schema = new_schema_name("cg_c9_feedback")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            context = service_context(session)
            app = FastAPI()
            app.include_router(feedback_router.router, prefix="/api/v1")
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/feedback",
                    json={
                        "contractId": str(uuid4()),
                        "targetType": "RISK",
                        "targetId": "risk-c9-1",
                        "verdict": "CORRECT",
                        "userCorrection": "风险判断正确。",
                        "reviewer": "法务主管",
                    },
                )
                assert response.status_code == 201
                data = response.json()["data"]
                assert data["targetType"] == "RISK"
                assert data["verdict"] == "CORRECT"

                records = await FeedbackRepository(session).list(TargetType.risk)
                assert len(records) == 1
                assert records[0].target_id == "risk-c9-1"
                assert records[0].user_comment == "风险判断正确。"

                invalid = await client.post(
                    "/api/v1/feedback",
                    json={
                        "contractId": str(uuid4()),
                        "targetType": "RISK",
                        "targetId": "risk-c9-2",
                        "verdict": "INVALID",
                    },
                )
                assert invalid.status_code == 422
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
