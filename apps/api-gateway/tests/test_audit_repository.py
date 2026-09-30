"""C49 audit_events 模型与 append-only 仓储测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import AuditAction, AuditOutcome
from app.db.repository.audit import AuditEventCreate, AuditRepository
from app.generated.schemas import Role
from tests.service_testkit import (
    ALEMBIC_INI,
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    session_factory,
)

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
FIXED_NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


def _alembic_config(dsn: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


async def _downgrade_to_base(dsn: str, schema: str) -> None:
    config = _alembic_config(dsn)
    engine = schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.downgrade(config, "base")

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


async def _table_names(dsn: str, schema: str) -> set[str]:
    engine: AsyncEngine = schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
            return set(names)
    finally:
        await engine.dispose()


def _event(
    *,
    action: AuditAction = AuditAction.login,
    outcome: AuditOutcome = AuditOutcome.success,
    created_at: datetime = FIXED_NOW,
    trace_id: str | None = "trace-1",
    resource_type: str = "AUTH",
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEventCreate:
    return AuditEventCreate(
        id=uuid4(),
        actor_id=uuid4(),
        actor_role=Role.admin,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        outcome=outcome,
        ip_address="127.0.0.1",
        user_agent="pytest",
        trace_id=trace_id,
        metadata=metadata or {},
        created_at=created_at,
    )


def test_audit_repository_is_append_only() -> None:
    assert hasattr(AuditRepository, "append")
    assert hasattr(AuditRepository, "list")
    assert not hasattr(AuditRepository, "update")
    assert not hasattr(AuditRepository, "delete")


def test_audit_metadata_shape() -> None:
    with pytest.raises(ValidationError):
        AuditEventCreate.model_validate(
            {
                "action": "LOGIN",
                "resource_type": "AUTH",
                "outcome": "SUCCESS",
                "metadata": [],
            }
        )


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C49 审计仓储 PostgreSQL 集成测试",
)
def test_audit_append_and_list() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_append_and_list(POSTGRES_DSN))


async def _exercise_append_and_list(dsn: str) -> None:
    schema = new_schema_name("cg_c49_append")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            repository = AuditRepository(session)
            created = await repository.append(
                _event(metadata={"contractId": "c-1", "count": 2})
            )
            await session.commit()

            records, total = await repository.list(page=1, page_size=20)
            assert total == 1
            assert records[0].id == created.id
            assert records[0].action is AuditAction.login
            assert records[0].outcome is AuditOutcome.success
            assert records[0].metadata == {"contractId": "c-1", "count": 2}
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C49 审计分页 PostgreSQL 集成测试",
)
def test_audit_filter_pagination() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_filter_pagination(POSTGRES_DSN))


async def _exercise_filter_pagination(dsn: str) -> None:
    schema = new_schema_name("cg_c49_filter")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            repository = AuditRepository(session)
            first = await repository.append(
                _event(
                    action=AuditAction.login,
                    created_at=FIXED_NOW,
                    trace_id="trace-login",
                )
            )
            second = await repository.append(
                _event(
                    action=AuditAction.upload,
                    created_at=FIXED_NOW + timedelta(seconds=1),
                    trace_id="trace-upload",
                    resource_type="CONTRACT",
                    resource_id="contract-1",
                )
            )
            third = await repository.append(
                _event(
                    action=AuditAction.export,
                    created_at=FIXED_NOW + timedelta(seconds=2),
                    trace_id="trace-export",
                    outcome=AuditOutcome.denied,
                )
            )
            await session.commit()

            page, total = await repository.list(page=2, page_size=2)
            assert total == 3
            assert [item.id for item in page] == [first.id]

            uploads, upload_total = await repository.list(
                action=AuditAction.upload,
                resource_type="CONTRACT",
                resource_id="contract-1",
            )
            assert upload_total == 1
            assert uploads[0].id == second.id

            traced, trace_total = await repository.list(trace_id="trace-export")
            assert trace_total == 1
            assert traced[0].id == third.id

            denied, denied_total = await repository.list(outcome=AuditOutcome.denied)
            assert denied_total == 1
            assert denied[0].id == third.id
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C49 审计迁移 PostgreSQL 集成测试",
)
def test_audit_migration_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_migration_roundtrip(POSTGRES_DSN))


async def _exercise_migration_roundtrip(dsn: str) -> None:
    schema = new_schema_name("cg_c49_migration")
    await create_schema(dsn, schema)
    try:
        await migrate(dsn, schema)
        assert "audit_events" in await _table_names(dsn, schema)
        await _downgrade_to_base(dsn, schema)
        assert "audit_events" not in await _table_names(dsn, schema)
        await migrate(dsn, schema)
        assert "audit_events" in await _table_names(dsn, schema)
    finally:
        await drop_schema(dsn, schema)