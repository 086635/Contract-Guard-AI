"""Redis MemoryStore 实现（延迟导入 redis）。"""

from __future__ import annotations

from typing import Any


class RedisMemoryStore:
    """Redis 字符串 KV 实现。"""

    def __init__(self, url: str, *, timeout_s: float = 0.2) -> None:
        import redis

        self._client: Any = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=timeout_s,
            socket_timeout=timeout_s,
        )
        self._client.ping()

    @staticmethod
    def _key(namespace: str, key: str) -> str:
        return f"{namespace}:{key}"

    def get(self, namespace: str, key: str) -> str | None:
        value = self._client.get(self._key(namespace, key))
        return None if value is None else str(value)

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_s: int | None = None,
    ) -> None:
        if ttl_s is None:
            self._client.set(self._key(namespace, key), value)
        else:
            self._client.set(
                self._key(namespace, key),
                value,
                ex=max(0, int(ttl_s)),
            )

    def delete(self, namespace: str, key: str) -> None:
        self._client.delete(self._key(namespace, key))

    def keys(self, namespace: str) -> list[str]:
        prefix = f"{namespace}:"
        return sorted(
            str(item)[len(prefix) :]
            for item in self._client.scan_iter(match=f"{prefix}*")
        )


__all__ = ["RedisMemoryStore"]
