"""C4 合同与任务仓储的 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.generated.schemas import ContractStatus, Status
from rag_contracts.enums import ContractType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
API_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = API_GATEWAY_ROOT / "alembic.ini"


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


async def _migrate(dsn: str, schema: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    engine = _schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


async def _exercise_repositories(dsn: str, schema: str) -> None:
    engine = _schema_engine(dsn, schema)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            contracts = ContractRepository(session)
            tasks = TaskRepository(session)

            created = await contracts.create(
                ContractCreate(
                    filename="采购合同.docx",
                    file_size=2048,
                    file_hash="c" * 64,
                    storage_path="contracts/purchase.docx",
                    contract_type=ContractType.PURCHASE,
                )
            )
            await session.commit()

            loaded = await contracts.get(created.id)
            assert loaded == created

            summaries, total = await contracts.list(page=1, page_size=10)
            assert total == 1
            assert len(summaries) == 1
            assert summaries[0].id == created.id

            before_update = loaded.updated_at
            await asyncio.sleep(0.01)
            await contracts.update_status(
                created.id,
                ContractStatus.parsing,
            )
            await session.commit()

            updated = await contracts.get(created.id)
            assert updated is not None
            assert updated.status is ContractStatus.parsing
            assert updated.updated_at > before_update

            task = await tasks.create(
                created.id,
                "PARSE_DOC",
                "celery-parse-1",
            )
            await session.commit()

            loaded_task = await tasks.get(task.id)
            assert loaded_task == task

            await tasks.update_progress(task.id, "parsing", 0.5)
            await session.commit()

            progressed = await tasks.get(task.id)
            assert progressed is not None
            assert progressed.stage == "parsing"
            assert progressed.progress == 0.5

            with pytest.raises(ValueError, match=r"0\.\.1"):
                await tasks.update_progress(task.id, "parsing", 1.1)

            await tasks.update_status(task.id, Status.done)
            await session.commit()

            finished = await tasks.get(task.id)
            assert finished is not None
            assert finished.status is Status.done
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=(
        "未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 合同/任务仓储集成测试（保持离线基线）"
    ),
)
def test_contract_and_task_repository_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    schema = f"cg_c4_{uuid4().hex}"

    _run(_create_schema(POSTGRES_DSN, schema))
    try:
        _run(_migrate(POSTGRES_DSN, schema))
        _run(_exercise_repositories(POSTGRES_DSN, schema))
    finally:
        _run(_drop_schema(POSTGRES_DSN, schema))
