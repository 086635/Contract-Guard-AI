"""Redis Pub/Sub 任务进度适配器。

设计要点：
1. channel 名唯一处：`cg_observability.task_progress_channel`；worker publish 与网关 subscribe 共用。
2. 订阅只转发 JSON 对象；收到 DONE/FAILED 后自然结束。
3. Redis/JSON 异常只记录并退出，不忙循环。

对应：`文件集合/codex/4.C8外部依赖适配器.md`。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import settings

# channel 命名唯一处已上移到 cg_observability（worker 不能 import app.*）
from cg_observability import get_logger, log_event, task_progress_channel

_LOG = get_logger("api_gateway.services.progress")


class RedisProgressBroker:
    """基于 Redis Pub/Sub 的进度订阅端口实现。"""

    def __init__(
        self,
        client: Redis | None = None,
        *,
        poll_timeout_s: float = 1.0,
    ) -> None:
        self._owns_client = client is None
        self._client = client or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self._poll_timeout_s = poll_timeout_s

    def subscribe(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """订阅任务进度事件。"""

        async def iterator() -> AsyncIterator[dict[str, Any]]:
            channel = task_progress_channel(task_id)
            pubsub = self._client.pubsub()
            try:
                await pubsub.subscribe(channel)
                while True:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=self._poll_timeout_s,
                    )
                    if message is None:
                        continue
                    payload = self._decode_message(message.get("data"))
                    if payload is None:
                        continue
                    yield payload
                    if payload.get("status") in {"DONE", "FAILED"}:
                        break
            except RedisError as exc:
                log_event(
                    _LOG,
                    "task_progress.subscribe_failed",
                    level="WARNING",
                    task_id=task_id,
                    error=repr(exc),
                )
            finally:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
                if self._owns_client:
                    await self._client.aclose()

        return iterator()

    @staticmethod
    def _decode_message(raw: object) -> dict[str, Any] | None:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if not isinstance(raw, str):
                raise TypeError(f"不支持的 message 类型: {type(raw).__name__}")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("任务进度消息必须是 JSON object")
            return payload
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            log_event(
                _LOG,
                "task_progress.invalid_message",
                level="WARNING",
                error=repr(exc),
            )
            return None


__all__ = [
    "RedisProgressBroker",
    "task_progress_channel",
]
