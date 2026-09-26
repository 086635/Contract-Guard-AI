"""Postgres MemoryStore 实现（延迟导入 psycopg）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


class PostgresMemoryStore:
    """Postgres 字符串 KV 实现。"""

    def __init__(self, dsn: str) -> None:
        import psycopg

        if not dsn:
            raise ValueError("postgres_dsn 不能为空")
        self._conn: Any = psycopg.connect(dsn)
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS cg_memory_kv (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NULL,
                    PRIMARY KEY (namespace, key)
                )
                """
            )
        self._conn.commit()

    def get(self, namespace: str, key: str) -> str | None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT value FROM cg_memory_kv
                WHERE namespace = %s AND key = %s
                  AND (expires_at IS NULL OR expires_at > NOW())
                """,
                (namespace, key),
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        ttl_s: int | None = None,
    ) -> None:
        expires_at = (
            None
            if ttl_s is None
            else datetime.now(UTC) + timedelta(seconds=max(0, int(ttl_s)))
        )
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO cg_memory_kv(namespace, key, value, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(namespace, key)
                DO UPDATE SET value = EXCLUDED.value,
                              expires_at = EXCLUDED.expires_at
                """,
                (namespace, key, value, expires_at),
            )
        self._conn.commit()

    def delete(self, namespace: str, key: str) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM cg_memory_kv WHERE namespace = %s AND key = %s",
                (namespace, key),
            )
        self._conn.commit()

    def keys(self, namespace: str) -> list[str]:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT key FROM cg_memory_kv
                WHERE namespace = %s
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY key
                """,
                (namespace,),
            )
            rows = cursor.fetchall()
        return [str(row[0]) for row in rows]


__all__ = ["PostgresMemoryStore"]
