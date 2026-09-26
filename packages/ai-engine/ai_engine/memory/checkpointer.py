"""LangGraph checkpointer 工厂。"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver

from cg_observability import get_logger, log_event

from .factory import MemorySettings

_LOG = get_logger("ai_engine.memory.checkpointer")


def build_checkpointer(settings: MemorySettings | None = None) -> Any:
    """默认 InMemorySaver；Postgres 不可用时降级。"""
    config = settings or MemorySettings()
    if config.backend != "postgres" or not config.postgres_dsn:
        return InMemorySaver()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        context_manager = PostgresSaver.from_conn_string(config.postgres_dsn)
        saver = context_manager.__enter__()
        saver.setup()
        return saver
    except Exception as exc:  # noqa: BLE001 - checkpointer 必须可降级
        log_event(
            _LOG,
            "memory.degrade",
            level="WARNING",
            reason=f"{type(exc).__name__}: {exc}",
            backend=config.backend,
        )
        return InMemorySaver()


__all__ = ["build_checkpointer"]
