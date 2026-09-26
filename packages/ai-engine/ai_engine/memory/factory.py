"""MemoryStore 配置与工厂。"""

from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from cg_observability import get_logger, log_event

from .store import InMemoryMemoryStore, MemoryStorePort

_LOG = get_logger("ai_engine.memory.factory")


class MemorySettings(BaseSettings):
    """记忆存储配置。"""

    model_config = SettingsConfigDict(
        env_prefix="CG_MEMORY_",
        env_file=".env",
        extra="ignore",
    )

    backend: Literal["inmemory", "redis", "postgres"] = "inmemory"
    redis_url: str = "redis://localhost:6379/3"
    postgres_dsn: str = ""
    ttl_s: int = 86400
    key_prefix: str = "cg:mem"


def _fallback(reason: str, backend: str) -> InMemoryMemoryStore:
    log_event(
        _LOG,
        "memory.degrade",
        level="WARNING",
        reason=reason,
        backend=backend,
    )
    return InMemoryMemoryStore()


def build_memory_store(
    settings: MemorySettings | None = None,
) -> MemoryStorePort:
    """按配置构造 store，外部依赖失败时降级 InMemory。"""
    config = settings or MemorySettings()
    if config.backend == "inmemory":
        return InMemoryMemoryStore()
    if config.backend == "redis":
        try:
            from .redis_store import RedisMemoryStore

            return RedisMemoryStore(config.redis_url)
        except Exception as exc:  # noqa: BLE001 - 可选后端必须降级
            return _fallback(f"{type(exc).__name__}: {exc}", config.backend)
    if config.backend == "postgres":
        try:
            from .postgres_store import PostgresMemoryStore

            return PostgresMemoryStore(config.postgres_dsn)
        except Exception as exc:  # noqa: BLE001 - 可选后端必须降级
            return _fallback(f"{type(exc).__name__}: {exc}", config.backend)
    return _fallback(f"unsupported backend: {config.backend}", config.backend)


__all__ = ["MemorySettings", "build_memory_store"]
