"""Service 层请求作用域上下文。

设计要点：
1. 用冻结 dataclass 固定单请求依赖，避免运行中替换。
2. 只承载抽象端口，不承载具体 SDK 客户端。
3. 所有 Service 共用同一构造入口，便于测试注入。

对应：`文件集合/codex/4.C7服务基座与依赖注入.md`。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ports import (
    ObjectStoragePort,
    ProgressBrokerPort,
    TaskDispatcherPort,
)


@dataclass(frozen=True, slots=True)
class ServiceContext:
    """单个请求的 Service 依赖集合。"""

    session: AsyncSession
    object_storage: ObjectStoragePort
    task_dispatcher: TaskDispatcherPort
    progress_broker: ProgressBrokerPort


__all__ = ["ServiceContext"]
