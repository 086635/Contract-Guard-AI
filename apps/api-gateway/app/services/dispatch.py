"""Celery 任务派发适配器。

设计要点：
1. 网关只按任务名发送，不 import worker 内部任务模块。
2. 任务名通过 `TaskKind` 受限，避免拼写漂移。
3. Celery client 可注入，测试不连接 broker。

对应：`文件集合/codex/4.C8外部依赖适配器.md`。
"""

from __future__ import annotations

from enum import StrEnum

from celery import Celery

from app.config import settings
from app.services.ports import TaskStatusSnapshot


class TaskKind(StrEnum):
    """网关允许派发的 worker 任务名。"""

    SCAN_UPLOAD = "scan_upload"
    CONVERT_DOC = "convert_doc"
    PARSE_DOC = "parse_doc"
    RUN_AUDIT = "run_audit"
    GEN_REPORT = "gen_report"
    REWRITE = "rewrite"


class CeleryTaskDispatcher:
    """基于 Celery client 的任务派发端口实现。"""

    def __init__(self, client: Celery | None = None) -> None:
        self._client = client or Celery(
            "contract_guard_gateway",
            broker=settings.celery_broker,
            backend=settings.celery_backend,
        )

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        """发送任务并返回 Celery task id。"""
        try:
            task_kind = TaskKind(task_name)
        except ValueError as exc:
            raise ValueError(f"未登记的任务名: {task_name}") from exc

        result = self._client.send_task(
            task_kind.value,
            args=list(args),
            task_id=task_id,
        )
        return str(result.id)

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        """读取 Celery backend 任务状态。"""
        task = self._client.AsyncResult(task_id)
        state = str(task.state)
        if state == "PENDING" and task.info is None:
            return None
        status = {
            "SUCCESS": "DONE",
            "FAILURE": "FAILED",
            "REVOKED": "FAILED",
            "STARTED": "RUNNING",
            "RETRY": "RUNNING",
        }.get(state, state)
        if status not in {"RUNNING", "DONE", "FAILED"}:
            status = "RUNNING"

        info = task.info if isinstance(task.info, dict) else {}
        stage = str(info.get("stage", "processing"))
        percent = float(info.get("percent", 0.0))
        result = info.get("result")
        if state == "SUCCESS" and result is None:
            result = info or None
        if result is not None and not isinstance(result, dict):
            result = {"value": result}
        return TaskStatusSnapshot(
            status=status,
            stage=stage,
            percent=percent,
            result=result,
        )


__all__ = [
    "CeleryTaskDispatcher",
    "TaskKind",
]
