"""Memory KV 端口与默认内存实现。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from cg_observability import get_logger, log_event

_LOG = get_logger("ai_engine.memory.store")
_VERSION = 1
_TModel = TypeVar("_TModel", bound=BaseModel)


class MemoryStorePort(Protocol):
    """字符串 KV 端口；业务模型序列化由 manager 负责。"""

    def get(self, namespace: str, key: str) -> str | None: ...

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_s: int | None = None,
    ) -> None: ...

    def delete(self, namespace: str, key: str) -> None: ...

    def keys(self, namespace: str) -> list[str]: ...


class InMemoryMemoryStore:
    """进程内 KV 实现，支持可注入时钟 TTL。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[tuple[str, str], tuple[str, float | None]] = {}

    def get(self, namespace: str, key: str) -> str | None:
        item = self._data.get((namespace, key))
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and self._clock() >= expires_at:
            self._data.pop((namespace, key), None)
            return None
        return value

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_s: int | None = None,
    ) -> None:
        expires_at = None if ttl_s is None else self._clock() + max(0, int(ttl_s))
        self._data[(namespace, key)] = (value, expires_at)

    def delete(self, namespace: str, key: str) -> None:
        self._data.pop((namespace, key), None)

    def keys(self, namespace: str) -> list[str]:
        self._purge_expired()
        return sorted(key for ns, key in self._data if ns == namespace)

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [
            item
            for item, (_, expires_at) in self._data.items()
            if expires_at is not None and now >= expires_at
        ]
        for item in expired:
            self._data.pop(item, None)


def encode_model(model: BaseModel) -> str:
    """将 Pydantic 模型编码为版本壳 JSON。"""
    return json.dumps(
        {"v": _VERSION, "data": model.model_dump(mode="json")},
        ensure_ascii=False,
    )


def decode_model(
    payload: str | None,
    model_type: type[_TModel],
) -> _TModel | None:
    """解码版本壳 JSON；未知版本与坏数据按 miss 处理。"""
    if payload is None:
        return None
    try:
        shell: Any = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        log_event(_LOG, "memory.decode.invalid", level="WARNING")
        return None
    if not isinstance(shell, dict) or shell.get("v") != _VERSION:
        log_event(
            _LOG,
            "memory.decode.unknown_version",
            level="WARNING",
            version=shell.get("v") if isinstance(shell, dict) else None,
        )
        return None
    data = shell.get("data")
    if not isinstance(data, dict):
        return None
    try:
        return model_type.model_validate(data)
    except ValidationError:
        log_event(_LOG, "memory.decode.validation_failed", level="WARNING")
        return None


__all__ = [
    "InMemoryMemoryStore",
    "MemoryStorePort",
    "decode_model",
    "encode_model",
]
