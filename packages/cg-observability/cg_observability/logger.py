"""单行 JSON 日志：全链路可观测性的输出载体（设计文档 §3.6.4）。

固定字段：`ts` / `level` / `logger` / `stage` / `trace_id` / `span_id` / `msg` + 业务字段。

示例输出：
```json
{"ts":"2026-08-29T02:11:03.221Z","level":"INFO","logger":"rag_core.service",
 "stage":"retrieve.done","trace_id":"9f1c...","span_id":"b2e1...","msg":"retrieve done",
 "candidates":40,"returned":5,"top_score":0.87,"token_count":1532,"took_ms":218}
```

关键实现：业务字段通过 `logging` 的 `extra` 传递，由 `JsonFormatter` 统一拼装，
保证无论调用方是否走 `log_event()`，输出都是**单行**、可被 Filebeat/Loki 直接采集的 JSON。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from .trace import get_span_id, get_trace_id

# 日志级别别名映射（兼容字符串与 logging 常量）
_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# 已配置过的 logger 名，避免重复挂 handler
_CONFIGURED: set[str] = set()


def resolve_level(default: str = "INFO") -> int:
    """解析全局日志级别：优先 `CG_RAG_LOG_LEVEL`，其次 `CG_LOG_LEVEL`。

    Args:
        default: 两者都未设置时的默认级别名。

    Returns:
        logging 级别常量。
    """
    raw = (os.environ.get("CG_RAG_LOG_LEVEL") or os.environ.get("CG_LOG_LEVEL") or default)
    return _LEVELS.get(raw.strip().upper(), logging.INFO)


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON（ensure_ascii=False 保留中文可读性）。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化单条日志。

        Args:
            record: logging 原生记录对象。

        Returns:
            单行 JSON 字符串（不含换行符）。
        """
        created = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict[str, Any] = {
            "ts": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "stage": getattr(record, "cg_stage", "") or "-",
            "trace_id": getattr(record, "cg_trace_id", "") or get_trace_id(),
            "span_id": getattr(record, "cg_span_id", "") or get_span_id(),
            "msg": record.getMessage(),
        }
        # 业务字段（extra 传入的 dict）
        fields = getattr(record, "cg_fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        # 异常信息：并入 msg 之后的独立字段，方便告警规则匹配
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else ""
            payload["exc_msg"] = str(record.exc_info[1]) if record.exc_info[1] else ""
        # ensure_ascii=False 保留中文；default=str 兜住不可序列化对象
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(name: str, *, level: int | None = None) -> logging.Logger:
    """获取（并按需初始化）一个输出单行 JSON 的 logger。

    幂等：同名 logger 不会重复挂 handler。

    Args:
        name: logger 名（推荐用模块全路径，如 `rag_core.service`）。
        level: 显式指定级别；为空则用 `resolve_level()`。

    Returns:
        已配置的 logging.Logger。
    """
    logger = logging.getLogger(name)
    if name not in _CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(level if level is not None else resolve_level())
        # 防止向 root logger 二次传播导致重复输出
        logger.propagate = False
        _CONFIGURED.add(name)
    return logger


def log_event(
    logger: logging.Logger,
    stage: str,
    *,
    level: str = "INFO",
    msg: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    **fields: Any,
) -> None:
    """打一条结构化事件日志（**推荐入口**）。

    Args:
        logger: `get_logger()` 返回的 logger。
        stage: 阶段名（如 `chunk.done` / `retrieve.dense`），用于日志检索与归因。
        level: 级别字符串，默认 INFO。
        msg: 人类可读摘要；为空则用 stage 兜底。
        trace_id: 显式指定 trace_id；为空则取当前上下文。
        span_id: 显式指定 span_id；为空则取当前上下文。
        **fields: 业务字段（必打字段见设计文档 §3.1.1 埋点清单）。
    """
    logger.log(
        _LEVELS.get(level.upper(), logging.INFO),
        msg or stage,
        extra={
            "cg_stage": stage,
            "cg_trace_id": trace_id or get_trace_id(),
            "cg_span_id": span_id or get_span_id(),
            "cg_fields": fields,
        },
    )


def configure_root(level: str | None = None) -> None:
    """为根 logger 挂上 JSON 输出，便于第三方库日志也走同一格式。

    Args:
        level: 级别名；为空则用 `resolve_level()`。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(_LEVELS.get((level or "").upper(), resolve_level()))


__all__ = [
    "JsonFormatter",
    "configure_root",
    "get_logger",
    "log_event",
    "resolve_level",
]
