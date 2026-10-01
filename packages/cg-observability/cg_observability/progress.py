"""任务进度 channel 命名（worker 与网关共用的跨进程契约）。

唯一处：`4.C14任务查询与SSE.md` 的 SSE 通道 / `4.C26端到端冒烟脚本.md` 依赖此命名。
worker 不能 import `app.*`，故该命名由网关上移到 `cg_observability`（两侧共同依赖的底层库），
worker 侧 `publish` 与网关侧 `subscribe` 共用同一个 `task_progress_channel()`。
"""

from __future__ import annotations

TASK_PROGRESS_CHANNEL_PREFIX = "task_progress"
TASK_HEARTBEAT_KEY_PREFIX = "task_heartbeat"


def task_progress_channel(task_id: str) -> str:
    """生成任务进度 redis pub/sub channel 名：`task_progress:{task_id}`。"""
    return f"{TASK_PROGRESS_CHANNEL_PREFIX}:{task_id}"


def task_heartbeat_key(task_id: str) -> str:
    """生成任务心跳 redis key：`task_heartbeat:{task_id}`（带 TTL 的存活标记）。

    说明（C63）：**进度是事件流，心跳是存活标记**，两者语义不同、不可合并——
    进度可能长时间没有新阶段，但 worker 仍活着；心跳缺失才说明任务失联。
    """
    return f"{TASK_HEARTBEAT_KEY_PREFIX}:{task_id}"


__all__ = [
    "TASK_HEARTBEAT_KEY_PREFIX",
    "TASK_PROGRESS_CHANNEL_PREFIX",
    "task_heartbeat_key",
    "task_progress_channel",
]
