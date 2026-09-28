"""Agent 条款改写派发 Service。

设计要点：
1. 网关只派发 Celery `rewrite`，不执行 LangGraph。
2. `xRequestId` 同时作为幂等缓存键和 Celery task id。
3. OpenAPI 要求完整 RewriteData，网关等待 broker 结果并设置明确超时。

对应：`文件集合/codex/4.C15Agent改写派发.md`。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from app.generated.schemas import AgentRewriteRequest, RewriteData
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind

_CACHE_LIMIT = 128
_REWRITE_CACHE: OrderedDict[str, RewriteData] = OrderedDict()


class AgentDispatchError(RuntimeError):
    """任务派发失败。"""


class AgentRewriteFailedError(RuntimeError):
    """Worker 明确返回失败。"""


class AgentRewriteTimeoutError(TimeoutError):
    """等待 RewriteData 超时。"""


class AgentService:
    """条款改写任务派发与结果等待。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.05,
    ) -> None:
        self._context = context
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    async def rewrite(self, body: AgentRewriteRequest) -> RewriteData:
        """派发 rewrite 并返回结构化改写结果。"""
        cached = _REWRITE_CACHE.get(body.x_request_id)
        if cached is not None:
            return cached.model_copy(deep=True)

        payload = body.model_dump(mode="json", by_alias=True)
        try:
            task_id = self._context.task_dispatcher.send(
                TaskKind.REWRITE,
                args=(payload,),
                task_id=body.x_request_id,
            )
        except Exception as exc:
            raise AgentDispatchError(str(exc)) from exc

        deadline = asyncio.get_running_loop().time() + self._timeout_s
        while True:
            snapshot = self._context.task_dispatcher.get_status(task_id)
            if snapshot is not None and snapshot.status == "FAILED":
                raise AgentRewriteFailedError(str(snapshot.result or "rewrite failed"))
            if (
                snapshot is not None
                and snapshot.status == "DONE"
                and snapshot.result is not None
            ):
                result = RewriteData.model_validate(snapshot.result)
                self._cache_put(body.x_request_id, result)
                return result.model_copy(deep=True)
            if asyncio.get_running_loop().time() >= deadline:
                raise AgentRewriteTimeoutError(
                    f"等待 rewrite 结果超过 {self._timeout_s} 秒"
                )
            await asyncio.sleep(self._poll_interval_s)

    @staticmethod
    def clear_cache() -> None:
        """清空进程内幂等缓存。"""
        _REWRITE_CACHE.clear()

    @staticmethod
    def _cache_put(key: str, value: RewriteData) -> None:
        _REWRITE_CACHE[key] = value
        _REWRITE_CACHE.move_to_end(key)
        while len(_REWRITE_CACHE) > _CACHE_LIMIT:
            _REWRITE_CACHE.popitem(last=False)


__all__ = [
    "AgentDispatchError",
    "AgentRewriteFailedError",
    "AgentRewriteTimeoutError",
    "AgentService",
]
