"""Server-Sent Events 序列化。

设计要点：
1. SSE 事件豁免 API 成功包络，直接发送 `TaskProgress`。
2. 事件 JSON 使用生成物 camelCase alias。
3. heartbeat 使用 SSE 注释行，不伪造进度。

对应：`文件集合/codex/4.C14任务查询与SSE.md`。
"""

from __future__ import annotations

import json
from typing import Any


def format_sse(
    payload: dict[str, Any],
    *,
    event: str = "progress",
) -> str:
    """把 JSON 对象格式化为一个 SSE 事件。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n"


def format_heartbeat() -> str:
    """返回 SSE 心跳注释。"""
    return ": ping\n\n"


__all__ = [
    "format_heartbeat",
    "format_sse",
]
