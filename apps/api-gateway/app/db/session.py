"""交付模块 C3：异步数据库引擎与会话工厂。

设计要点：
1. engine 与 `SessionLocal` 是 PostgreSQL 连接的唯一定义处。
2. DSN 复用 `app.config.settings.postgres_dsn`，不在模块内散落读取环境变量。
3. `expire_on_commit=False` 保证提交后仍可读取 ORM 对象字段。

对应：`文件集合/codex/4.C3引擎与会话.md`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(settings.postgres_dsn, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """提供 FastAPI 数据库会话依赖。

    Yields:
        每个请求独立、退出上下文时自动关闭的异步会话。
    """
    async with SessionLocal() as session:
        yield session


__all__ = [
    "SessionLocal",
    "engine",
    "get_session",
]
