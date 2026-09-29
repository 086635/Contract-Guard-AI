"""Service 集成测试共享工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.services.context import ServiceContext
from app.services.ports import (
    ObjectStoragePort,
    ProgressBrokerPort,
    TaskDispatcherPort,
    TaskStatusSnapshot,
)

API_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = API_GATEWAY_ROOT / "alembic.ini"


async def create_schema(dsn: str, schema: str) -> None:
    """创建随机测试 schema。"""
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def drop_schema(dsn: str, schema: str) -> None:
    """销毁随机测试 schema。"""
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


def schema_engine(dsn: str, schema: str) -> AsyncEngine:
    """构造固定 search_path 的测试 engine。"""
    return create_async_engine(
        dsn,
        connect_args={"server_settings": {"search_path": schema}},
    )


async def migrate(dsn: str, schema: str) -> None:
    """在测试 schema 中执行 Alembic upgrade head。"""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    engine = schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


class _NullTaskDispatcher:
    """未注入时的无副作用任务派发替身。

    读路径（如 `IngestionService.finalize_parse` / `TaskService.get`）会调用
    `get_status`，故默认值必须实现 `TaskDispatcherPort`，而不是裸 `object()`。
    """

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        del task_name, args, task_id
        raise NotImplementedError("测试未注入 task_dispatcher")

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        del task_id
        return None


def service_context(
    session: AsyncSession,
    *,
    object_storage: ObjectStoragePort | None = None,
    task_dispatcher: TaskDispatcherPort | None = None,
    progress_broker: ProgressBrokerPort | None = None,
) -> ServiceContext:
    """构造仅使用真实 session 的测试上下文。"""
    return ServiceContext(
        session=session,
        object_storage=object_storage or cast(ObjectStoragePort, object()),
        task_dispatcher=task_dispatcher or _NullTaskDispatcher(),
        progress_broker=progress_broker or cast(ProgressBrokerPort, object()),
    )


def new_schema_name(prefix: str) -> str:
    """生成短测试 schema 名。"""
    return f"{prefix}_{uuid4().hex}"


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """构造测试 session factory。"""
    return async_sessionmaker(engine, expire_on_commit=False)


def rule_body(*, version: int = 1) -> dict[str, Any]:
    """构造合法配置请求体。"""
    return {
        "id": str(uuid4()),
        "contractType": "PURCHASE",
        "promptConfig": {
            "roleName": "采购合同审查员",
            "systemPrompt": "检查付款与违约金条款。",
            "temperature": 0.0,
            "riskToleranceLevel": "BALANCED",
        },
        "enabledRules": ["PENALTY_TOO_HIGH"],
        "enabled": True,
        "reviewThresholds": {"high": 0.85, "medium": 0.65, "low": 0.40},
        "customEntities": [],
        "updatedAt": "2026-09-27T00:00:00Z",
        "version": version,
    }
