"""C9 配置规则真实化测试。"""

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
    install_test_auth,
    migrate,
    new_schema_name,
    rule_body,
    schema_engine,
    service_context,
    session_factory,
)

from app.api.deps import get_service_context
from app.api.v1 import config as config_router
from app.db.repository.config_feedback import RuleRepository, RuleUpsert

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def test_config_router_has_no_mock_import() -> None:
    path = Path(__file__).resolve().parents[1] / "config.py"
    text = path.read_text(encoding="utf-8")

    assert "MockDataFactory" not in text


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 配置服务集成测试",
)
def test_config_routes_use_repository() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_config_routes(POSTGRES_DSN))


async def _exercise_config_routes(dsn: str) -> None:
    schema = new_schema_name("cg_c9_config")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            context = service_context(session)
            app = FastAPI()
            app.include_router(config_router.router, prefix="/api/v1")
            install_test_auth(app, session)
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                rule_id = uuid4()
                payload = rule_body()
                payload["id"] = str(rule_id)
                await RuleRepository(session).upsert(
                    RuleUpsert(
                        id=rule_id,
                        rule_name="采购合同审查员",
                        category="PURCHASE",
                        prompt_template="检查付款与违约金条款。",
                        threshold=0.5,
                        is_enabled=True,
                        review_thresholds={"high": 0.85, "medium": 0.65, "low": 0.40},
                    )
                )
                await session.commit()

                created = await client.put(
                    f"/api/v1/settings/rules/{rule_id}",
                    json=payload,
                )
                assert created.status_code == 200
                created_data = created.json()["data"]
                assert created_data["version"] == 2
                payload["version"] = created_data["version"]
                assert created_data["id"] == str(rule_id)
                assert created_data["promptConfig"]["roleName"] == "采购合同审查员"
                assert created_data["enabled"] is True
                assert created_data["reviewThresholds"] == {
                    "high": 0.85,
                    "medium": 0.65,
                    "low": 0.40,
                }

                listed = await client.get(
                    "/api/v1/settings/rules",
                    params={"contractType": "PURCHASE"},
                )
                assert listed.status_code == 200
                assert [item["id"] for item in listed.json()["data"]] == [str(rule_id)]

                fetched = await client.get(f"/api/v1/settings/rules/{rule_id}")
                assert fetched.status_code == 200
                assert fetched.json()["data"] == created_data

                missing = await client.get(f"/api/v1/settings/rules/{uuid4()}")
                assert missing.status_code == 404

                invalid = await client.put(
                    f"/api/v1/settings/rules/{rule_id}",
                    json={
                        **payload,
                        "reviewThresholds": {
                            "high": 0.50,
                            "medium": 0.80,
                            "low": 0.40,
                        },
                    },
                )
                assert invalid.status_code == 422

                disabled = await client.put(
                    f"/api/v1/settings/rules/{rule_id}",
                    json={
                        **payload,
                        "enabled": False,
                        "reviewThresholds": {
                            "high": 0.80,
                            "medium": 0.60,
                            "low": 0.30,
                        },
                    },
                )
                assert disabled.status_code == 200
                assert disabled.json()["data"]["enabled"] is False
                assert disabled.json()["data"]["reviewThresholds"] == {
                    "high": 0.80,
                    "medium": 0.60,
                    "low": 0.30,
                }

                conflict = await client.put(
                    f"/api/v1/settings/rules/{rule_id}",
                    json={**payload, "version": 2},
                )
                assert conflict.status_code == 409
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
