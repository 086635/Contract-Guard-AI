"""C44 用户仓储与 Alembic 迁移测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.repository.auth import (
    UserAlreadyExistsError,
    UserCreate,
    UserRepository,
)
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


def test_role_enum_guard() -> None:
    assert {role.value for role in Role} == {"ADMIN", "REVIEWER", "VIEWER"}
    with pytest.raises(ValueError):
        Role("ROOT")


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C44 用户仓储 PostgreSQL 集成测试",
)
def test_user_repository_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_user_repository(POSTGRES_DSN))


async def _exercise_user_repository(dsn: str) -> None:
    schema = new_schema_name("cg_c44_users")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            repository = UserRepository(session)
            created = await repository.create(
                UserCreate(
                    username=" Alice.Admin ",
                    password_hash="argon2-hash",
                    role=Role.admin,
                    display_name="Alice",
                )
            )
            await session.commit()

            assert created.username == "alice.admin"
            assert created.role is Role.admin
            assert created.is_active is True
            assert created.display_name == "Alice"
            assert await repository.get(created.id) == created
            assert await repository.get_by_username("ALICE.ADMIN") == created

            viewer = await repository.create(
                UserCreate(
                    username="viewer",
                    password_hash="argon2-hash-2",
                    role=Role.viewer,
                    display_name="Viewer",
                )
            )
            await session.commit()
            listed, total = await repository.list(page=1, page_size=1)
            assert total == 2
            assert len(listed) == 1
            assert listed[0].username == "alice.admin"

            admins, admin_total = await repository.list(role=Role.admin)
            assert admin_total == 1
            assert [item.id for item in admins] == [created.id]

            updated = await repository.update(
                viewer.id,
                role=Role.reviewer,
                is_active=False,
                display_name="Reviewer",
            )
            await session.commit()
            assert updated.role is Role.reviewer
            assert updated.is_active is False
            assert updated.display_name == "Reviewer"
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C44 用户名唯一约束 PostgreSQL 集成测试",
)
def test_duplicate_username_rejected() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_duplicate_username(POSTGRES_DSN))


async def _exercise_duplicate_username(dsn: str) -> None:
    schema = new_schema_name("cg_c44_duplicate")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            repository = UserRepository(session)
            await repository.create(
                UserCreate(
                    username="same-user",
                    password_hash="hash-1",
                    role=Role.viewer,
                    display_name="Same User",
                )
            )
            await session.commit()
            with pytest.raises(UserAlreadyExistsError):
                await repository.create(
                    UserCreate(
                        username=" SAME-USER ",
                        password_hash="hash-2",
                        role=Role.viewer,
                        display_name="Duplicate",
                    )
                )
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C44 Alembic roundtrip PostgreSQL 集成测试",
)
def test_auth_migration_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_auth_migration(POSTGRES_DSN))


async def _exercise_auth_migration(dsn: str) -> None:
    schema = new_schema_name("cg_c44_migration")
    await create_schema(dsn, schema)
    try:
        await migrate(dsn, schema)
        assert "users" in await _table_names(dsn, schema)
        await _downgrade_to_base(dsn, schema)
        assert "users" not in await _table_names(dsn, schema)
        await migrate(dsn, schema)
        assert "users" in await _table_names(dsn, schema)
    finally:
        await drop_schema(dsn, schema)