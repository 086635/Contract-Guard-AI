"""全链路追踪上下文（trace_id / span_id）。

设计目标：从 API 入口到 LLM 调用，同一 `trace_id` 能串联全部日志。

实现方式：`contextvars.ContextVar` —— 相比 threading.local，
它能正确穿透 `asyncio` 与线程池（`ThreadPoolExecutor` 需显式拷贝上下文，
`asyncio.to_thread` 与 `copy_context().run()` 会自动携带）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator
from uuid import uuid4

# trace_id：全链路唯一，跨进程/跨服务透传
_TRACE_ID: ContextVar[str] = ContextVar("cg_trace_id", default="")
# span_id：当前所处调用段，用于区分同一次 trace 内的不同阶段
_SPAN_ID: ContextVar[str] = ContextVar("cg_span_id", default="")


def new_trace_id() -> str:
    """生成新的全链路追踪 ID（UUID4 字符串）。"""
    return str(uuid4())


def new_span_id() -> str:
    """生成新的段 ID（16 位十六进制，短小便于人眼定位）。"""
    return uuid4().hex[:16]


def get_trace_id() -> str:
    """读取当前上下文的 trace_id；未设置时返回空串。"""
    return _TRACE_ID.get()


def get_span_id() -> str:
    """读取当前上下文的 span_id；未设置时返回空串。"""
    return _SPAN_ID.get()


def set_trace_id(trace_id: str) -> Token[str]:
    """设置 trace_id。

    Args:
        trace_id: 追踪 ID。

    Returns:
        用于 `reset_trace_id()` 还原的 Token。
    """
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token: Token[str]) -> None:
    """还原 trace_id 到设置前的值。

    Args:
        token: `set_trace_id()` 返回的 Token。
    """
    _TRACE_ID.reset(token)


def ensure_trace_id(trace_id: str | None = None) -> str:
    """解析出生效的 trace_id（**不修改上下文**）。

    解析顺序：显式入参 → 当前上下文已有的 → 新建 UUID4。
    本函数是**纯函数**式的解析：绑定动作由 `TraceContext.__enter__` / `Span.__enter__` /
    `bind_trace_id()` 负责，避免"仅创建对象就污染外层上下文"。

    Args:
        trace_id: 调用方传入的 trace_id；为空则复用当前上下文的，仍为空才新建。

    Returns:
        最终生效的 trace_id。
    """
    if trace_id:
        return trace_id
    return _TRACE_ID.get() or new_trace_id()


def bind_trace_id(trace_id: str | None = None) -> str:
    """解析并把 trace_id 绑定到当前上下文（如在 API 入口调用一次）。

    Args:
        trace_id: 调用方传入的 trace_id；为空则新建。

    Returns:
        已绑定的 trace_id。
    """
    resolved = trace_id or _TRACE_ID.get() or new_trace_id()
    _TRACE_ID.set(resolved)
    return resolved


class Span:
    """一个可计时的调用段（span）。

    用法：
        with Span("retrieve.dense") as span:
            ...
            span.set(attribute="value")
        print(span.elapsed_ms)
    """

    def __init__(self, name: str, *, trace_id: str | None = None) -> None:
        """初始化 span。

        Args:
            name: 段名（如 `retrieve.dense`）。
            trace_id: 指定 trace_id；为空则沿用当前上下文。
        """
        self.name: str = name
        self.trace_id: str = ensure_trace_id(trace_id)
        self.span_id: str = new_span_id()
        self.attributes: dict[str, Any] = {}
        self.elapsed_ms: int = 0
        self._start: float = 0.0
        self._span_token: Token[str] | None = None
        self._trace_token: Token[str] | None = None

    def set(self, **attributes: Any) -> Span:
        """写入 span 属性（如 `candidates=20`）。

        Args:
            **attributes: 任意可序列化的键值。

        Returns:
            self，便于链式调用。
        """
        self.attributes.update(attributes)
        return self

    def __enter__(self) -> Span:
        """进入 span：绑定 span_id（必要时一并绑定 trace_id）并开始计时。"""
        if not _TRACE_ID.get():
            self._trace_token = _TRACE_ID.set(self.trace_id)
        self._span_token = _SPAN_ID.set(self.span_id)
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出 span：停止计时并还原上下文。"""
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
        if self._span_token is not None:
            _SPAN_ID.reset(self._span_token)
            self._span_token = None
        if self._trace_token is not None:
            _TRACE_ID.reset(self._trace_token)
            self._trace_token = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入日志的字典。"""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "span": self.name,
            "elapsed_ms": self.elapsed_ms,
            **self.attributes,
        }


class TraceContext:
    """一次完整调用（如一次检索 / 一次审计）的追踪上下文。

    用法（推荐，能自动绑定 trace_id 并统计总耗时）：
        with TraceContext(trace_id="...") as ctx:
            with ctx.span("embed") as sp:
                ...
            print(ctx.elapsed_ms, ctx.to_dict())

    也支持不进入上下文直接使用：`TraceContext().span("x")`。
    """

    def __init__(self, trace_id: str | None = None, name: str = "request") -> None:
        """初始化追踪上下文。

        Args:
            trace_id: 外部传入的 trace_id（如来自 HTTP Header `X-Request-ID`）。
            name: 本次调用的可读名称。
        """
        self.name: str = name
        self.trace_id: str = ensure_trace_id(trace_id)
        self.spans: list[Span] = []
        self._start: float = 0.0
        self.elapsed_ms: int = 0
        self._token: Token[str] | None = None

    def span(self, name: str) -> Span:
        """创建一个属于本上下文的 span（已进入上下文时自动登记）。

        Args:
            name: 段名。

        Returns:
            新的 Span 对象（尚未开始计时，需 `with` 或手动 enter）。
        """
        sp = Span(name, trace_id=self.trace_id)
        self.spans.append(sp)
        return sp

    def __enter__(self) -> TraceContext:
        """进入上下文：绑定 trace_id 并开始整体计时。"""
        self._token = _TRACE_ID.set(self.trace_id)
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出上下文：停止计时并还原 trace_id。"""
        self.elapsed_ms = int((time.perf_counter() - self._start) * 1000)
        if self._token is not None:
            _TRACE_ID.reset(self._token)
            self._token = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入日志的字典（含各 span 耗时）。"""
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "elapsed_ms": self.elapsed_ms,
            "spans": [sp.to_dict() for sp in self.spans],
        }


@contextmanager
def trace_context(trace_id: str | None = None, name: str = "request") -> Iterator[TraceContext]:
    """轻量版上下文管理器：临时绑定 trace_id。

    Args:
        trace_id: 外部传入的 trace_id。
        name: 调用名称。

    Yields:
        已绑定 trace_id 的 TraceContext。
    """
    ctx = TraceContext(trace_id=trace_id, name=name)
    with ctx:
        yield ctx


__all__ = [
    "Span",
    "TraceContext",
    "bind_trace_id",
    "ensure_trace_id",
    "get_span_id",
    "get_trace_id",
    "new_span_id",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "trace_context",
]
