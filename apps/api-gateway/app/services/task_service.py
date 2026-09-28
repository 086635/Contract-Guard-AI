"""任务状态查询与 SSE 订阅 Service。

设计要点：
1. DB task 行提供任务身份，Celery backend 快照可覆盖实时状态。
2. SSE 先发送当前快照，再转发 Redis 进度事件。
3. DONE/FAILED 后主动结束订阅。

对应：`文件集合/codex/4.C14任务查询与SSE.md`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from pydantic import ValidationError

from app.db.repository.contract import TaskRecord, TaskRepository
from app.generated.schemas import Status, TaskProgress
from app.services.context import ServiceContext
from app.services.sse import format_sse
from cg_observability import get_logger, log_event

_LOG = get_logger("api_gateway.services.task")
_TERMINAL_STATUSES = {Status.done, Status.failed}


class TaskNotFoundError(LookupError):
    """任务不存在。"""


class TaskService:
    """任务状态读取与事件流。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._tasks = TaskRepository(context.session)

    async def get(self, task_id: UUID) -> TaskProgress | None:
        """读取任务当前快照。"""
        record = await self._tasks.get(task_id)
        snapshot = self._context.task_dispatcher.get_status(str(task_id))
        if record is None and snapshot is None:
            return None

        status = record.status if record is not None else Status.running
        stage = record.stage if record is not None else "processing"
        percent = record.progress if record is not None else 0.0
        result = self._record_result(record)
        if snapshot is not None:
            try:
                status = Status(snapshot.status)
            except ValueError:
                status = Status.running
            stage = snapshot.stage
            percent = min(1.0, max(0.0, snapshot.percent))
            result = snapshot.result or result
        return TaskProgress(
            taskId=str(task_id),
            stage=stage,
            percent=percent,
            status=status,
            result=result,
        )

    async def stream(self, task_id: UUID) -> AsyncIterator[str]:
        """发送当前快照并持续转发进度事件。"""
        current = await self.get(task_id)
        if current is None:
            raise TaskNotFoundError(str(task_id))

        yield format_sse(
            current.model_dump(mode="json", by_alias=True),
        )
        if current.status in _TERMINAL_STATUSES:
            return

        async for raw_event in self._context.progress_broker.subscribe(str(task_id)):
            try:
                event = TaskProgress.model_validate(raw_event)
            except ValidationError as exc:
                log_event(
                    _LOG,
                    "task.stream.invalid_event",
                    level="WARNING",
                    task_id=str(task_id),
                    error=repr(exc),
                )
                continue
            yield format_sse(event.model_dump(mode="json", by_alias=True))
            if event.status in _TERMINAL_STATUSES:
                break

    @staticmethod
    def _record_result(record: TaskRecord | None) -> dict[str, object] | None:
        if record is None:
            return None
        result: dict[str, object] = {"contractId": str(record.contract_id)}
        if record.error_message:
            result["errorMessage"] = record.error_message
        return result


__all__ = [
    "TaskNotFoundError",
    "TaskService",
]
