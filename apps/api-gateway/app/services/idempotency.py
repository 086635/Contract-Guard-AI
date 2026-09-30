"""C52 Redis 幂等键、请求体哈希与响应快照适配器。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis

from app.config import settings


class IdempotencyStoreError(RuntimeError):
    """Redis 幂等存储不可用。"""


class IdempotencyConflictError(IdempotencyStoreError):
    """同一 requestId 对应了不同请求体。"""


class IdempotencyInProgressError(IdempotencyStoreError):
    """同一 requestId 的首次请求仍在执行。"""


class IdempotencySnapshot(BaseModel):
    """可重放的响应快照；不包含 Authorization/Cookie。"""

    model_config = ConfigDict(extra="forbid")

    body_hash: str
    status_code: int
    body: str
    body_encoding: Literal["utf-8", "base64"]
    content_type: str
    created_at: datetime


def canonical_body_hash(body: bytes) -> str:
    """对 JSON body 做稳定规范化后计算 SHA-256；非 JSON 使用原始字节。"""
    try:
        parsed = json.loads(body.decode("utf-8") or "null")
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        canonical = body
    return hashlib.sha256(canonical).hexdigest()


def idempotency_key(operation_id: str, user_key: str, request_id: str) -> str:
    """构造统一 Redis 幂等键。"""
    return f"cg:idempotency:{operation_id}:{user_key}:{request_id}"


class IdempotencyStore:
    """Redis 幂等存储适配器。"""

    def __init__(
        self,
        client: Redis | None = None,
        *,
        ttl_seconds: int = 24 * 60 * 60,
    ) -> None:
        self._client = client or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self._ttl_seconds = ttl_seconds

    async def begin(
        self,
        key: str,
        body_hash: str,
    ) -> tuple[IdempotencySnapshot | None, bool]:
        """返回已有快照或获取首次执行锁。"""
        existing = await self._client.get(key)
        if existing:
            return _parse_existing(existing, body_hash)

        lock_payload = json.dumps(
            {"in_progress": True, "body_hash": body_hash},
            separators=(",", ":"),
        )
        acquired = await self._client.set(
            key,
            lock_payload,
            nx=True,
            ex=self._ttl_seconds,
        )
        if acquired:
            return None, True

        existing = await self._client.get(key)
        if not existing:
            raise IdempotencyStoreError("idempotency lock disappeared")
        return _parse_existing(existing, body_hash)

    async def complete(self, key: str, snapshot: IdempotencySnapshot) -> None:
        """写入可重放响应快照。"""
        await self._client.set(
            key,
            snapshot.model_dump_json(),
            ex=self._ttl_seconds,
        )

    async def release(self, key: str) -> None:
        """释放未完成的首次执行锁。"""
        await self._client.delete(key)


def _parse_existing(
    raw: str,
    body_hash: str,
) -> tuple[IdempotencySnapshot | None, bool]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise IdempotencyStoreError("invalid idempotency payload") from exc

    if data.get("in_progress"):
        if data.get("body_hash") != body_hash:
            raise IdempotencyConflictError("request id reused with different body")
        raise IdempotencyInProgressError("request is still in progress")

    try:
        snapshot = IdempotencySnapshot.model_validate(data)
    except Exception as exc:
        raise IdempotencyStoreError("invalid idempotency snapshot") from exc
    if snapshot.body_hash != body_hash:
        raise IdempotencyConflictError("request id reused with different body")
    return snapshot, False


def snapshot_from_response(
    body_hash: str,
    status_code: int,
    body: bytes,
    content_type: str,
) -> IdempotencySnapshot:
    """把 HTTP 响应体投影为安全快照。"""
    try:
        text = body.decode("utf-8")
        encoding: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeDecodeError:
        import base64

        text = base64.b64encode(body).decode("ascii")
        encoding = "base64"
    return IdempotencySnapshot(
        body_hash=body_hash,
        status_code=status_code,
        body=text,
        body_encoding=encoding,
        content_type=content_type,
        created_at=datetime.now(UTC),
    )


def response_body_bytes(snapshot: IdempotencySnapshot) -> bytes:
    """从快照恢复原始响应字节。"""
    if snapshot.body_encoding == "base64":
        import base64

        return base64.b64decode(snapshot.body.encode("ascii"))
    return snapshot.body.encode("utf-8")


__all__ = [
    "IdempotencyConflictError",
    "IdempotencyInProgressError",
    "IdempotencySnapshot",
    "IdempotencyStore",
    "IdempotencyStoreError",
    "canonical_body_hash",
    "idempotency_key",
    "response_body_bytes",
    "snapshot_from_response",
]