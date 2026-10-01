"""C2 Alembic 迁移基线的离线与 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from collections.abc import Coroutine
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.db.models import Base

EXPECTED_TABLES = {
    "audit_events",
    "audit_reports",
    "audit_risks",
    "audit_tasks",
    "auth_sessions",
    "contract_elements",
    "contract_commits",
    "contract_previews",
    "contracts",
    "seal_detections",
    "settings_rules",
    "user_feedbacks",
    "users",
}

API_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = API_GATEWAY_ROOT / "alembic.ini"
OFFLINE_DSN = "postgresql+asyncpg://cg:cg@localhost:5432/cg"
POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def _alembic_config(dsn: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def _run_offline(action: str, revision: str) -> str:
    config = _alembic_config(OFFLINE_DSN)
    buffer = StringIO()
    with redirect_stdout(buffer):
        if action == "upgrade":
            command.upgrade(config, revision, sql=True)
        else:
            command.downgrade(config, revision, sql=True)
    return buffer.getvalue()


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


async def _create_schema(dsn: str, schema: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def _drop_schema(dsn: str, schema: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


def _schema_engine(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn,
        connect_args={"server_settings": {"search_path": schema}},
    )


async def _migrate(dsn: str, schema: str, revision: str) -> None:
    config = _alembic_config(dsn)
    engine = _schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, revision)

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


async def _downgrade(dsn: str, schema: str) -> None:
    config = _alembic_config(dsn)
    engine = _schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.downgrade(config, "base")

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


async def _list_tables(dsn: str, schema: str) -> set[str]:
    engine = _schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = :schema_name"),
                {"schema_name": schema},
            )
            return {row[0] for row in result}
    finally:
        await engine.dispose()


def test_offline_upgrade_creates_all_contract_tables() -> None:
    sql = _run_offline("upgrade", "head")
    created_tables = set(re.findall(r"CREATE TABLE ([a-z_]+)", sql))

    assert EXPECTED_TABLES <= created_tables
    assert created_tables - EXPECTED_TABLES <= {"alembic_version"}


def test_offline_downgrade_drops_all_contract_tables() -> None:
    sql = _run_offline("downgrade", "head:base")
    dropped_tables = set(re.findall(r"DROP TABLE ([a-z_]+)", sql))

    assert EXPECTED_TABLES <= dropped_tables


def test_migration_schema_matches_c1_metadata() -> None:
    sql = _run_offline("upgrade", "head")
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(sql)
        for table_name in EXPECTED_TABLES:
            actual_columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            expected_columns = set(Base.metadata.tables[table_name].columns.keys())
            assert actual_columns == expected_columns

            actual_indexes = {
                row[1] for row in connection.execute(f"PRAGMA index_list({table_name})")
            }
            expected_indexes = {
                index.name for index in Base.metadata.tables[table_name].indexes
            }
            assert expected_indexes <= actual_indexes
    finally:
        connection.close()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=("未设置 CG_POSTGRES_DSN，跳过 PostgreSQL Alembic 集成测试（保持离线基线）"),
)
def test_postgres_upgrade_downgrade() -> None:
    assert POSTGRES_DSN is not None
    schema = f"cg_c2_{uuid4().hex}"

    _run(_create_schema(POSTGRES_DSN, schema))
    try:
        _run(_migrate(POSTGRES_DSN, schema, "head"))
        tables_after_upgrade = _run(_list_tables(POSTGRES_DSN, schema))
        assert EXPECTED_TABLES <= tables_after_upgrade

        _run(_downgrade(POSTGRES_DSN, schema))
        tables_after_downgrade = _run(_list_tables(POSTGRES_DSN, schema))
        assert EXPECTED_TABLES.isdisjoint(tables_after_downgrade)
    finally:
        _run(_drop_schema(POSTGRES_DSN, schema))
