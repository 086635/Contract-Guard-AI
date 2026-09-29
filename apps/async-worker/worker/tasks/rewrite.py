"""条款改写 Celery 任务（C41 / `4-6解析审核桥接.md`）。

设计要点：
1. Worker 只负责反序列化请求并调用唯一 `rewrite_clause()`。
2. 成功返回 OpenAPI `RewriteData` 的 camelCase JSON。
3. 校验/模型/Graph 异常向 Celery 抛出，网关将其映射为 FAILED 或超时。
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError

from ai_engine.agents import (
    AgentRewriteRequest,
    RewriteUnavailableError,
    rewrite_clause,
)
from cg_observability import get_logger, log_event
from worker.main import celery_app

_LOG = get_logger("worker.tasks.rewrite")


@celery_app.task(name="rewrite", bind=True)
def rewrite_task(self, payload: dict[str, Any]) -> dict[str, Any]:
    """执行条款改写并返回 JSON 可序列化的 `RewriteData`。"""
    del self  # bind=True 保留 Celery task context，业务只消费 payload
    trace_id = str(payload.get("xRequestId") or uuid.uuid4().hex)

    try:
        request = AgentRewriteRequest.model_validate(payload)
        response = rewrite_clause(request)
        result = response.model_dump(mode="json", by_alias=True)
        if result.get("taskId") != request.xRequestId:
            raise RewriteUnavailableError(
                "rewrite taskId 与 xRequestId 不一致",
                trace_id=trace_id,
            )
        log_event(
            _LOG,
            "rewrite.task.done",
            trace_id=trace_id,
            contract_id=request.contractId,
            clause_id=request.clauseId,
        )
        return result
    except Exception as exc:
        error = (
            exc
            if isinstance(exc, (RewriteUnavailableError, ValidationError))
            else RewriteUnavailableError(
                f"rewrite task failed: {type(exc).__name__}: {exc}",
                trace_id=trace_id,
            )
        )
        log_event(
            _LOG,
            "rewrite.task.fail",
            level="ERROR",
            trace_id=trace_id,
            error=repr(error),
        )
        raise error from exc


__all__ = ["rewrite_task"]