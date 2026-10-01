"""cg-observability：全链路可观测性（trace / log / token）。

**零强制依赖**：仅用 stdlib；`tiktoken` 与 `structlog` 为可选增强，缺失时自动回落。

三个能力：
1. `trace`  —— trace_id / span_id 透传（contextvars，穿透 asyncio 与线程池）
2. `logger` —— 单行 JSON 日志，固定携带 trace_id
3. `token_counter` —— Token 计数（tiktoken 优先 / 字符估算兜底）+ 分段耗时统计
4. `progress`  —— 任务进度 redis channel 命名（worker 与网关共用的跨进程契约）
"""

from __future__ import annotations

from .logger import JsonFormatter, configure_root, get_logger, log_event, resolve_level
from .progress import (
    TASK_HEARTBEAT_KEY_PREFIX,
    TASK_PROGRESS_CHANNEL_PREFIX,
    task_heartbeat_key,
    task_progress_channel,
)
from .token_counter import (
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_MODEL,
    TokenCounter,
    TokenMeter,
)
from .trace import (
    Span,
    TraceContext,
    bind_trace_id,
    ensure_trace_id,
    get_span_id,
    get_trace_id,
    new_span_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    trace_context,
)

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_MODEL",
    "TASK_HEARTBEAT_KEY_PREFIX",
    "TASK_PROGRESS_CHANNEL_PREFIX",
    "JsonFormatter",
    "Span",
    "TokenCounter",
    "TokenMeter",
    "TraceContext",
    "__version__",
    "bind_trace_id",
    "configure_root",
    "ensure_trace_id",
    "get_logger",
    "get_span_id",
    "get_trace_id",
    "log_event",
    "new_span_id",
    "new_trace_id",
    "reset_trace_id",
    "resolve_level",
    "set_trace_id",
    "task_heartbeat_key",
    "task_progress_channel",
    "trace_context",
]
