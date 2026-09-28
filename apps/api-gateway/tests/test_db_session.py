"""C3 引擎与会话的离线构造及 PostgreSQL 连通测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import SessionLocal, engine, get_session

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


async def _select_one_via_dependency() -> int:
    async for session in get_session():
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("SELECT 1"))
        return result.scalar_one()
    raise AssertionError("get_session() 未产出会话")


def test_engine_and_sessionmaker_are_constructible() -> None:
    assert engine.url.drivername == "postgresql+asyncpg"
    assert isinstance(SessionLocal, async_sessionmaker)
    assert SessionLocal.kw["expire_on_commit"] is False


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=("未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 会话集成测试（保持离线基线）"),
)
def test_get_session_select_one() -> None:
    assert _run(_select_one_via_dependency()) == 1
