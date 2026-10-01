"""网关 Service 层外部依赖端口。

设计要点：
1. Service 只依赖 Protocol，不直接 import MinIO/Celery/Redis SDK。
2. 端口只暴露网关实际使用的最小方法，避免泄漏 worker 实现。
3. 测试可用 Fake 对象替换端口，保持依赖注入边界。

对应：`文件集合/codex/4.C7服务基座与依赖注入.md`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class StoredObjectInfo:
    """对象存储元数据。"""

    size: int
    etag: str


@dataclass(frozen=True, slots=True)
class TaskStatusSnapshot:
    """Celery backend 任务状态快照。"""

    status: str
    stage: str
    percent: float
    result: dict[str, Any] | None = None


class ObjectStoragePort(Protocol):
    """对象存储端口。"""

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        """生成 PUT 预签名 URL。"""
        ...

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        """生成 GET 预签名 URL。"""
        ...

    def stat_object(self, file_key: str) -> StoredObjectInfo:
        """读取对象大小与 ETag。"""
        ...

    def download_object(self, file_key: str, target_path: str) -> str:
        """把对象下载到本地路径（服务端内部读取派生文件用）。"""
        ...


class TaskDispatcherPort(Protocol):
    """Celery 任务派发端口。"""

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        """派发任务并返回 task id。"""
        ...

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        """读取任务状态快照。"""
        ...


class ProgressBrokerPort(Protocol):
    """任务进度订阅端口。"""

    def subscribe(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """订阅指定任务的进度事件。"""
        ...


__all__ = [
    "ObjectStoragePort",
    "ProgressBrokerPort",
    "StoredObjectInfo",
    "TaskDispatcherPort",
    "TaskStatusSnapshot",
]
