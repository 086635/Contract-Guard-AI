"""Alembic 异步迁移环境。

设计要点：
1. 迁移元数据直接复用 C1 的 `Base.metadata`，不另建模型定义。
2. 在线迁移优先使用 Alembic 配置 URL，否则回退 `CG_POSTGRES_DSN`。
3. 测试可注入已有连接，避免在临时 schema 外误触业务表。

对应：`文件集合/codex/4.C2Alembic迁移基线.md`。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """返回迁移连接地址。"""
    configured_url = config.get_main_option("sqlalchemy.url")
    if configured_url:
        return configured_url
    return settings.postgres_dsn


def run_migrations_offline() -> None:
    """离线模式生成迁移 SQL，不建立数据库连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定连接上执行迁移。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建 async engine 并执行在线迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """执行在线迁移；测试注入连接时不另建事件循环。"""
    injected_connection = config.attributes.get("connection")
    if injected_connection is not None:
        do_run_migrations(injected_connection)
        return

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
