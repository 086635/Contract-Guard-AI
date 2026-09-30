"""C50 AuditService 采集、脱敏与管理员查询测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user, get_db
from app.api.v1 import audit as audit_router
from app.db.models import AuditAction, AuditOutcome
from app.generated.schemas import Role
from app.services.audit_service import AuditService, sanitize_metadata
from app.services.auth_service import AuthService
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    session_factory,
)

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


class _Nested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _FakeSession:
    def begin_nested(self) -> _Nested:
        return _Nested()

    async def commit(self) -> None:
        return None


def test_audit_action_coverage() -> None:
    required = {
        "LOGIN",
        "UPLOAD",
        "VIEW",
        "REWRITE",
        "COMMIT",
        "REPORT",
        "SETTINGS_UPDATE",
        "EXPORT",
        "AUTH_DENIED",
    }
    assert required <= {action.value for action in AuditAction}


def test_audit_service_redacts_metadata() -> None:
    sanitized = sanitize_metadata(
        {
            "contractId": "contract-1",
            "count": 2,
            "password": "plain-text",
            "prompt": "contract body",
            "unknown": "drop-me",
        }
    )

    assert sanitized == {
        "contractId": "contract-1",
        "count": 2,
        "password": "[REDACTED]",
        "prompt": "[REDACTED]",
    }


def test_audit_failure_does_not_break_business(monkeypatch: pytest.MonkeyPatch) -> None:
    service = AuditService(_FakeSession())  # type: ignore[arg-type]

    async def failing_append(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr(service._repository, "append", failing_append)
    _run(
        service.write_event(
            AuditAction.login,
            "AUTH",
            AuditOutcome.success,
            metadata={"contractId": "c-1"},
        )
    )


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C50 AuditService PostgreSQL 集成测试",
)
def test_audit_service_query_by_trace_and_resource() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_audit_query(POSTGRES_DSN))


async def _exercise_audit_query(dsn: str) -> None:
    schema = new_schema_name("cg_c50_query")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            service = AuditService(session)
            await service.write_event(
                AuditAction.view,
                "CONTRACT",
                AuditOutcome.success,
                actor_id=uuid4(),
                actor_role=Role.admin,
                resource_id="contract-1",
                trace_id="trace-c50",
                metadata={"contractId": "contract-1", "content": "secret body"},
            )
            await session.commit()

            records, total = await service.list(
                trace_id="trace-c50",
                resource_id="contract-1",
            )
            assert total == 1
            assert records[0].trace_id == "trace-c50"
            assert records[0].resource_id == "contract-1"
            assert records[0].metadata["content"] == "[REDACTED]"
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C50 管理员审计查询 PostgreSQL 集成测试",
)
def test_audit_query_admin_only() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_admin_only(POSTGRES_DSN))


async def _exercise_admin_only(dsn: str) -> None:
    schema = new_schema_name("cg_c50_admin")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            auth_service = AuthService(session)
            admin = await auth_service.create_user("audit-admin", "admin-pass", Role.admin, "Audit Admin")
            viewer = await auth_service.create_user("audit-viewer", "viewer-pass", Role.viewer, "Audit Viewer")
            await session.commit()

            app = FastAPI()
            app.include_router(audit_router.router, prefix="/api/v1")

            async def override_get_db():
                yield session

            app.dependency_overrides[get_db] = override_get_db
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                app.dependency_overrides[get_current_user] = lambda: viewer
                denied = await client.get("/api/v1/audit/events")
                assert denied.status_code == 403

                app.dependency_overrides[get_current_user] = lambda: admin
                allowed = await client.get("/api/v1/audit/events")
                assert allowed.status_code == 200
                assert allowed.json()["data"]["total"] >= 1
                assert allowed.json()["data"]["items"][0]["action"] == "AUTH_DENIED"
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)