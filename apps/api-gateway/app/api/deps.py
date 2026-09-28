"""FastAPI Service 层依赖注入。

设计要点：
1. `get_db()` 复用 C3 的唯一 session 生成器。
2. 具体外部适配器延迟到 C8，并允许测试通过 dependency override 替换。
3. 路由只依赖 `get_service_context()`，不自行构造外部客户端。

对应：`文件集合/codex/4.C7服务基座与依赖注入.md`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.context import ServiceContext
from app.services.ports import (
    ObjectStoragePort,
    ProgressBrokerPort,
    TaskDispatcherPort,
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """提供单个请求的数据库会话。"""
    async for session in get_session():
        yield session


def get_object_storage() -> ObjectStoragePort:
    """延迟构造对象存储适配器。"""
    from app.services.storage import MinioObjectStorage

    return MinioObjectStorage()


def get_task_dispatcher() -> TaskDispatcherPort:
    """延迟构造任务派发适配器。"""
    from app.services.dispatch import CeleryTaskDispatcher

    return CeleryTaskDispatcher()


def get_progress_broker() -> ProgressBrokerPort:
    """延迟构造进度订阅适配器。"""
    from app.services.progress import RedisProgressBroker

    return RedisProgressBroker()


async def get_service_context(
    db: Annotated[AsyncSession, Depends(get_db)],
    object_storage: Annotated[ObjectStoragePort, Depends(get_object_storage)],
    task_dispatcher: Annotated[TaskDispatcherPort, Depends(get_task_dispatcher)],
    progress_broker: Annotated[ProgressBrokerPort, Depends(get_progress_broker)],
) -> ServiceContext:
    """组装单请求 Service 上下文。"""
    return ServiceContext(
        session=db,
        object_storage=object_storage,
        task_dispatcher=task_dispatcher,
        progress_broker=progress_broker,
    )


__all__ = [
    "get_db",
    "get_object_storage",
    "get_progress_broker",
    "get_service_context",
    "get_task_dispatcher",
]
