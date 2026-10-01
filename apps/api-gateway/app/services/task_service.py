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

from app.db.repository.auth import UserRecord
from app.db.repository.contract import TaskRecord, TaskRepository
from app.generated.schemas import Status, StreamTokenData, TaskProgress
from app.services.context import ServiceContext
from app.services.security import JwtService, StreamTokenClaims
from app.services.sse import format_sse
from cg_observability import get_logger, log_event

_LOG = get_logger("api_gateway.services.task")
_TERMINAL_STATUSES = {Status.done, Status.failed}


def _celery_lookup_id(record: TaskRecord | None, task_id: UUID) -> str:
    """Celery 状态 / 进度 channel 的查询键：优先该任务行登记的 `celery_task_id`。"""
    if record is not None and record.celery_task_id:
        return record.celery_task_id
    return str(task_id)


class TaskNotFoundError(LookupError):
    """任务不存在。"""


class StreamTokenScopeError(PermissionError):
    """stream token 合法但不属于该任务（跨任务复用）。"""


class TaskService:
    """任务状态读取与事件流。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        jwt: JwtService | None = None,
        tasks: TaskRepository | None = None,
    ) -> None:
        """构造服务；`jwt` / `tasks` 可注入（测试用固定时钟与内存仓储，保持离线可测）。"""
        self._context = context
        self._tasks = tasks or TaskRepository(context.session)
        self._jwt = jwt or JwtService.from_settings()

    async def assert_access(
        self,
        task_id: UUID,
        user: UserRecord | None = None,
    ) -> TaskRecord:
        """任务访问权（**唯一处**）：任务不存在 → `TaskNotFoundError`（路由映射 404，不泄露存在性）。

        当前口径：单租户私有化部署下，**已认证用户可见全部合同**，因此这里只校验任务存在。
        `user` 是预留的归属扩展位：一旦 `contracts` 增加归属字段（母文档 `4-7 §⑤.11` 的
        `created_by_id`，目前尚未落库），**只改这一处**即可收紧为「仅任务归属人可见」。
        """
        record = await self._tasks.get(task_id)
        if record is None:
            raise TaskNotFoundError(str(task_id))
        return record

    async def issue_stream_token(
        self,
        task_id: UUID,
        user: UserRecord,
    ) -> StreamTokenData:
        """签发短期 SSE 订阅令牌（60 秒，绑定 userId + taskId）。"""
        await self.assert_access(task_id, user)
        token, expires_in = self._jwt.issue_stream(user_id=user.id, task_id=task_id)
        return StreamTokenData(streamToken=token, expiresIn=expires_in)

    def verify_stream_token(self, task_id: UUID, token: str) -> StreamTokenClaims:
        """校验短期订阅令牌，并确认它**绑定在本任务**上。

        Raises:
            TokenValidationError: 签名/过期/类型不对（含把 access token 当 stream token 用）。
            StreamTokenScopeError: 令牌合法但绑定的 taskId 与请求路径不一致。
        """
        claims = self._jwt.verify_stream(token)
        if claims.task_id != task_id:
            raise StreamTokenScopeError("stream token 不属于该任务")
        return claims

    async def get(self, task_id: UUID) -> TaskProgress | None:
        """读取任务当前快照。"""
        record = await self._tasks.get(task_id)
        snapshot = self._context.task_dispatcher.get_status(
            _celery_lookup_id(record, task_id)
        )
        if record is None and snapshot is None:
            return None

        status = record.status if record is not None else Status.running
        stage = record.stage if record is not None else "processing"
        percent = record.progress if record is not None else 0.0
        result = self._record_result(record)
        if snapshot is not None:
            result_status = (
                snapshot.result.get("status")
                if isinstance(snapshot.result, dict)
                else None
            )
            try:
                status = Status(snapshot.status)
            except ValueError:
                status = Status.running
            if snapshot.status == "FAILED" or result_status == "FAILED":
                status = Status.failed
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

        record = await self._tasks.get(task_id)
        async for raw_event in self._context.progress_broker.subscribe(
            _celery_lookup_id(record, task_id)
        ):
            try:
                event = TaskProgress.model_validate(raw_event).model_copy(
                    update={"task_id": str(task_id)}
                )
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
    "StreamTokenScopeError",
    "TaskNotFoundError",
    "TaskService",
]
