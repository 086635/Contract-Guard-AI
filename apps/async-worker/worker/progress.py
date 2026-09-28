"""任务进度上报（Celery `update_state` 封装）。

一个文件、三张**各自独立**的阶段表：
  · `PARSE_STAGES` / `ParseProgressReporter` —— 解析链路（`3-6`，T-C7）
  · `AUDIT_STAGES` / `AuditProgressReporter` —— 审核链路（`2-6`，T-B8）
  · `REPORT_STAGES` / `ReportProgressReporter` —— 报告链路（`4.C20`，T-D3）

🔴 **不得把阶段表合并成一个全局阶段枚举**：不同链路的阶段名会撞（如都有 `extract`），
合并后 "extract 到底是解析、审核还是报告" 无法回答（标准 §0.7）。
meta 结构统一对齐 openapi `TaskProgress`：`{taskId, stage, percent, status, result}`。
"""

from __future__ import annotations

import json
import os
from typing import Any

from ai_engine.graphs.state import AUDIT_STAGES
from cg_observability import get_logger, log_event, task_progress_channel

_LOG = get_logger("worker.progress")

_PROGRESS_REDIS_URL = os.getenv("CG_REDIS_URL", "redis://localhost:6379/0")
_redis_client: Any = None
_redis_ready = False
_publish_disabled = False


def _progress_redis() -> Any:
    """惰性构造进度发布用 redis 客户端；构造失败置 None。"""
    global _redis_client, _redis_ready
    if not _redis_ready:
        _redis_ready = True
        try:
            import redis

            _redis_client = redis.Redis.from_url(
                _PROGRESS_REDIS_URL,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
        except Exception:  # noqa: BLE001 - 无 redis 依赖时静默降级
            _redis_client = None
    return _redis_client


def _publish_progress(task: Any, meta: dict[str, Any], trace_id: str) -> None:
    """把进度 meta publish 到 `task_progress:{celery_task_id}`（失败只告警、不阻断任务）。"""
    global _publish_disabled
    if _publish_disabled:
        return
    task_id = getattr(getattr(task, "request", None), "id", None)
    if not task_id:
        return
    client = _progress_redis()
    if client is None:
        _publish_disabled = True
        return
    try:
        client.publish(
            task_progress_channel(str(task_id)), json.dumps(meta, ensure_ascii=False)
        )
    except Exception as exc:  # noqa: BLE001 - 进度发布失败不影响任务本身
        _publish_disabled = True
        log_event(
            _LOG,
            "progress.publish_failed",
            level="WARNING",
            trace_id=trace_id,
            error=repr(exc),
        )


#: 解析链路阶段（顺序即执行顺序；`3-6 §5.5`）
PARSE_STAGES: tuple[str, ...] = (
    "fetch",
    "route",
    "parse_text",
    "seal",
    "extract",
    "index",
)

#: 阶段 → 百分比（**唯一处**；`parse_text` 最耗时：逐页 OCR + 全文装配）
PARSE_STAGE_PERCENT: dict[str, float] = {
    "fetch": 0.05,
    "route": 0.10,
    "parse_text": 0.55,
    "seal": 0.70,
    "extract": 0.85,
    "index": 0.95,
}

AUDIT_STAGE_PERCENT: dict[str, float] = {
    "classify": 0.10,
    "extract": 0.25,
    "risk": 0.60,
    "conflict": 0.75,
    "standard_compare": 0.88,
    "report_draft": 0.95,
}

#: 报告链路阶段（顺序即执行顺序；C20）
REPORT_STAGES: tuple[str, ...] = (
    "render",
    "upload",
)

#: 报告阶段 → 百分比（唯一处；渲染后进入上传，上传成功后由 finish 置 1.0）
REPORT_STAGE_PERCENT: dict[str, float] = {
    "render": 0.60,
    "upload": 0.95,
}


class ParseProgressReporter:
    """解析任务进度上报（meta 对齐 openapi `TaskProgress`）。

    设计要点：
    1. **单调不回退**：重跑阶段不会让前端进度条倒退（`update_state` 前比大小）。
    2. **非法阶段名只告警**：阶段名必须 ∈ `PARSE_STAGES`，否则记 warning 并忽略。
    3. **不吞异常**：`update_state` 失败（如无 broker）只告警，不影响任务本身。
    """

    def __init__(self, task: Any, contract_id: str, trace_id: str) -> None:
        """初始化。

        Args:
            task: Celery 任务实例（`bind=True` 的 `self`）。
            contract_id: 合同 ID。
            trace_id: 追踪 ID。
        """
        self._task = task
        self._contract_id = contract_id
        self._trace_id = trace_id
        self._percent = 0.0

    @property
    def percent(self) -> float:
        """当前百分比（0~1）。"""
        return self._percent

    def start(self) -> None:
        """任务开始：`RUNNING` / `stage="start"` / `percent=0.0`。"""
        self._emit("start", 0.0, "RUNNING")

    def stage(self, name: str) -> None:
        """按**阶段表**上报。

        Args:
            name: 阶段名（必须 ∈ `PARSE_STAGES`）。
        """
        if name not in PARSE_STAGE_PERCENT:
            _LOG.warning("未知解析阶段：%s（已忽略）", name)
            return
        self._emit(name, PARSE_STAGE_PERCENT[name], "RUNNING")

    def finish(self, result: dict | None = None) -> None:
        """任务完成：`DONE` / `percent=1.0`。"""
        self._emit("done", 1.0, "DONE", result=result)

    def fail(self, reason: str) -> None:
        """任务失败：`FAILED`，**停在当前百分比**（不虚报 1.0）。"""
        self._emit("failed", self._percent, "FAILED", result={"reason": reason})

    # ---------------- 内部 ----------------
    def _emit(
        self, stage: str, percent: float, status: str, result: dict | None = None
    ) -> None:
        """写入 Celery 任务状态（单调保护 + 失败只告警）。"""
        target = max(self._percent, float(percent))
        self._percent = target
        meta: dict[str, Any] = {
            "taskId": self._contract_id,
            "stage": stage,
            "percent": round(target, 4),
            "status": status,
            "result": result,
        }
        try:
            self._task.update_state(state=status, meta=meta)
        except Exception as exc:  # noqa: BLE001 - 上报失败（无 broker / 后端不可用）只告警
            # ⚠️ 字段名不能叫 `stage`：`log_event(logger, stage, ...)` 的第二个位置参数
            # 已经占用了 `stage`，同名关键字会 TypeError（实测踩过）→ 用 `task_stage`。
            log_event(
                _LOG,
                "progress.update_failed",
                level="WARNING",
                trace_id=self._trace_id,
                task_stage=stage,
                error=repr(exc),
            )
        _publish_progress(self._task, meta, self._trace_id)


class AuditProgressReporter:
    """审核任务进度上报。"""

    def __init__(self, task: Any, contract_id: str, trace_id: str) -> None:
        self._task = task
        self._contract_id = contract_id
        self._trace_id = trace_id
        self._percent = 0.0

    @property
    def percent(self) -> float:
        return self._percent

    def start(self) -> None:
        self._emit("classify", 0.0, "RUNNING")

    def stage(self, name: str) -> None:
        if name not in AUDIT_STAGE_PERCENT:
            _LOG.warning("未知审核阶段：%s（已忽略）", name)
            return
        self._emit(name, AUDIT_STAGE_PERCENT[name], "RUNNING")

    def finish(self, result: dict | None = None) -> None:
        self._emit("done", 1.0, "DONE", result=result)

    def fail(self, reason: str) -> None:
        self._emit("failed", self._percent, "FAILED", result={"reason": reason})

    def _emit(
        self,
        stage: str,
        percent: float,
        status: str,
        result: dict | None = None,
    ) -> None:
        target = max(self._percent, float(percent))
        self._percent = target
        meta: dict[str, Any] = {
            "taskId": self._contract_id,
            "stage": stage,
            "percent": round(target, 4),
            "status": status,
            "result": result,
        }
        try:
            self._task.update_state(state=status, meta=meta)
        except Exception as exc:  # noqa: BLE001 - 上报失败只告警
            log_event(
                _LOG,
                "progress.update_failed",
                level="WARNING",
                trace_id=self._trace_id,
                task_stage=stage,
                error=repr(exc),
            )
        _publish_progress(self._task, meta, self._trace_id)


class ReportProgressReporter:
    """报告任务进度上报。"""

    def __init__(self, task: Any, contract_id: str, trace_id: str) -> None:
        self._task = task
        self._contract_id = contract_id
        self._trace_id = trace_id
        self._percent = 0.0

    @property
    def percent(self) -> float:
        return self._percent

    def start(self) -> None:
        self._emit("render", 0.0, "RUNNING")

    def stage(self, name: str) -> None:
        if name not in REPORT_STAGE_PERCENT:
            _LOG.warning("未知报告阶段：%s（已忽略）", name)
            return
        self._emit(name, REPORT_STAGE_PERCENT[name], "RUNNING")

    def finish(self, result: dict | None = None) -> None:
        self._emit("done", 1.0, "DONE", result=result)

    def fail(self, reason: str) -> None:
        self._emit("failed", self._percent, "FAILED", result={"reason": reason})

    def _emit(
        self,
        stage: str,
        percent: float,
        status: str,
        result: dict | None = None,
    ) -> None:
        target = max(self._percent, float(percent))
        self._percent = target
        meta: dict[str, Any] = {
            "taskId": self._contract_id,
            "stage": stage,
            "percent": round(target, 4),
            "status": status,
            "result": result,
        }
        try:
            self._task.update_state(state=status, meta=meta)
        except Exception as exc:  # noqa: BLE001 - 上报失败只告警
            log_event(
                _LOG,
                "progress.update_failed",
                level="WARNING",
                trace_id=self._trace_id,
                task_stage=stage,
                error=repr(exc),
            )
        _publish_progress(self._task, meta, self._trace_id)


__all__ = [
    "AUDIT_STAGES",
    "AUDIT_STAGE_PERCENT",
    "PARSE_STAGES",
    "PARSE_STAGE_PERCENT",
    "REPORT_STAGES",
    "REPORT_STAGE_PERCENT",
    "AuditProgressReporter",
    "ParseProgressReporter",
    "ReportProgressReporter",
]
