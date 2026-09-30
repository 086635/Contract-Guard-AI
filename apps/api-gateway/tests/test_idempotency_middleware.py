"""C52 Redis 幂等中间件矩阵测试。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.api.middlewares import IdempotencyMiddleware
from app.config import settings
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencySnapshot,
    IdempotencyStore,
    IdempotencyStoreError,
    canonical_body_hash,
    snapshot_from_response,
)


class _FakeStore:
    """内存幂等存储，行为与 Redis 版本保持一致。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.records: dict[str, dict[str, Any]] = {}
        self.begin_calls = 0

    async def begin(self, key: str, body_hash: str) -> tuple[IdempotencySnapshot | None, bool]:
        self.begin_calls += 1
        if self.fail:
            raise IdempotencyStoreError("redis unavailable")
        current = self.records.get(key)
        if current is not None:
            if current.get("in_progress"):
                raise IdempotencyInProgressError("request in progress")
            if current["body_hash"] != body_hash:
                raise IdempotencyConflictError("different body")
            return IdempotencySnapshot.model_validate(current["snapshot"]), False
        self.records[key] = {"in_progress": True, "body_hash": body_hash}
        return None, True

    async def complete(self, key: str, snapshot: IdempotencySnapshot) -> None:
        self.records[key] = {
            "in_progress": False,
            "body_hash": snapshot.body_hash,
            "snapshot": snapshot.model_dump(mode="json"),
        }

    async def release(self, key: str) -> None:
        self.records.pop(key, None)


def _build_app(store: _FakeStore, *, calls: list[int]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, store=store)  # type: ignore[arg-type]

    @app.post("/api/v1/contracts/upload", status_code=201)
    async def idempotent_upload(request: Request) -> JSONResponse:
        calls[0] += 1
        return JSONResponse({"executions": calls[0]})

    @app.post("/api/v1/other")
    async def non_idempotent(request: Request) -> JSONResponse:
        calls[0] += 1
        return JSONResponse({"executions": calls[0]})

    return app


def test_idempotent_replay() -> None:
    store = _FakeStore()
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))
    request_id = str(uuid4())
    headers = {"X-Request-ID": request_id}
    body = {"fileKey": "contracts/a.docx"}

    first = client.post("/api/v1/contracts/upload", headers=headers, json=body)
    second = client.post("/api/v1/contracts/upload", headers=headers, json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert calls[0] == 1
    assert first.headers["X-Request-ID"] == request_id


def test_same_key_different_body_conflict() -> None:
    store = _FakeStore()
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))
    request_id = str(uuid4())
    headers = {"X-Request-ID": request_id}

    first = client.post(
        "/api/v1/contracts/upload",
        headers=headers,
        json={"fileKey": "contracts/a.docx"},
    )
    conflict = client.post(
        "/api/v1/contracts/upload",
        headers=headers,
        json={"fileKey": "contracts/b.docx"},
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ERR_DUPLICATE_REQUEST"
    assert calls[0] == 1


def test_redis_down_fails_closed() -> None:
    store = _FakeStore(fail=True)
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))

    response = client.post(
        "/api/v1/contracts/upload",
        headers={"X-Request-ID": str(uuid4())},
        json={"fileKey": "contracts/a.docx"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "ERR_INTERNAL"
    assert response.json()["detail"] == "idempotency store unavailable"
    assert calls[0] == 0


def test_non_idempotent_route_bypasses() -> None:
    store = _FakeStore()
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))
    request_id = str(uuid4())

    first = client.post("/api/v1/other", headers={"X-Request-ID": request_id})
    second = client.post("/api/v1/other", headers={"X-Request-ID": request_id})

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls[0] == 2
    assert store.begin_calls == 0


def test_missing_request_id_is_validation_error() -> None:
    store = _FakeStore()
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))

    response = client.post("/api/v1/contracts/upload", json={"fileKey": "contracts/a.docx"})

    assert response.status_code == 422
    assert response.json()["code"] == "ERR_VALIDATION"
    assert response.json()["detail"] == "X-Request-ID is required"
    assert calls[0] == 0


def test_invalid_request_id_is_validation_error() -> None:
    store = _FakeStore()
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))

    response = client.post(
        "/api/v1/contracts/upload",
        headers={"X-Request-ID": "not-a-uuid"},
        json={"fileKey": "contracts/a.docx"},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "ERR_VALIDATION"
    assert response.json()["detail"] == "X-Request-ID must be a UUID"
    assert calls[0] == 0


def test_in_progress_request_conflicts() -> None:
    store = _FakeStore()
    calls = [0]
    client = TestClient(_build_app(store, calls=calls))
    request_id = str(uuid4())
    headers = {"X-Request-ID": request_id}
    body = {"fileKey": "contracts/a.docx"}

    first = client.post("/api/v1/contracts/upload", headers=headers, json=body)
    assert first.status_code == 200
    key = next(iter(store.records))
    store.records[key]["in_progress"] = True

    conflict = client.post("/api/v1/contracts/upload", headers=headers, json=body)

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "ERR_DUPLICATE_REQUEST"
    assert conflict.json()["detail"] == "request is still in progress"
    assert calls[0] == 1


def test_real_redis_store_roundtrip() -> None:
    async def exercise() -> None:
        client = Redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await client.ping()
        except RedisError:
            await client.aclose()
            return
        key = f"cg:c52:test:{uuid4()}"
        body_hash = canonical_body_hash(b"{\"a\":1}")
        store = IdempotencyStore(client=client, ttl_seconds=60)
        try:
            snapshot, acquired = await store.begin(key, body_hash)
            assert snapshot is None and acquired is True
            await store.complete(
                key,
                snapshot_from_response(body_hash, 200, b"{\"ok\":true}", "application/json"),
            )
            replay, acquired_again = await store.begin(key, body_hash)
            assert acquired_again is False
            assert replay is not None and replay.body == '{"ok":true}'
        finally:
            await client.delete(key)
            await client.aclose()

    asyncio.run(exercise())