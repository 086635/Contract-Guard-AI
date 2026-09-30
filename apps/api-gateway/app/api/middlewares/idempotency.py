"""C52 Redis 幂等中间件与请求 ID 处理。

仅对声明了 X-Request-ID 的写操作启用 Redis 幂等：
- SET NX + 24h TTL 防止并发首次请求双执行；
- 同 key 同 body 重放原始响应；
- 同 key 不同 body 返回 409 ERR_DUPLICATE_REQUEST；
- Redis 不可用返回 503 fail-closed。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.generated.schemas import ApiError, ErrorCode
from app.services.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencySnapshot,
    IdempotencyStore,
    IdempotencyStoreError,
    canonical_body_hash,
    idempotency_key,
    response_body_bytes,
    snapshot_from_response,
)
from app.services.security import JwtService, TokenValidationError
from cg_observability import reset_trace_id, set_trace_id

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_UUID_SEGMENT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_IDEMPOTENT_ROUTES = {
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/contracts/presign"),
    ("POST", "/api/v1/contracts/upload"),
    ("POST", "/api/v1/contracts/{}/commit"),
    ("POST", "/api/v1/agent/rewrite"),
    ("POST", "/api/v1/report/{}/generate"),
    ("POST", "/api/v1/feedback"),
    ("PUT", "/api/v1/settings/rules/{}"),
    ("POST", "/api/v1/admin/users"),
    ("PATCH", "/api/v1/admin/users/{}"),
}
_REQUEST_ID_REQUIRED_ROUTES = _IDEMPOTENT_ROUTES | {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
}


def _normalize_path(path: str) -> str:
    segments: list[str] = []
    for segment in path.strip("/").split("/"):
        if segment.isdigit() or _UUID_SEGMENT.match(segment):
            segments.append("{}")
        else:
            segments.append(segment)
    return "/" + "/".join(segments)


def _trace_id(request: Request, fallback: str) -> str:
    from cg_observability import get_trace_id

    return get_trace_id() or request.headers.get("X-Trace-Id") or fallback


def _error_response(
    *,
    status_code: int,
    code: ErrorCode,
    message: str,
    detail: str,
    trace_id: str,
    request_id: str,
) -> JSONResponse:
    payload = ApiError(
        code=code,
        message=message,
        detail=detail,
        traceId=trace_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(by_alias=True, mode="json", exclude_none=True),
        headers={"X-Request-ID": request_id, "X-Trace-Id": trace_id},
    )


async def _consume_response_body(response: Response) -> tuple[bytes, Response]:
    """读取响应体并重建 Response，以便缓存快照。"""
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body, response
    chunks = [chunk async for chunk in response.body_iterator]
    raw = b"".join(chunks)
    rebuilt = Response(
        content=raw,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
        background=response.background,
    )
    return raw, rebuilt


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Redis 幂等中间件。"""

    def __init__(
        self,
        app: Any,
        *,
        store: IdempotencyStore | None = None,
    ) -> None:
        super().__init__(app)
        self._store = store or IdempotencyStore()

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        method = request.method.upper()
        normalized_path = _normalize_path(request.url.path)
        route_key = (method, normalized_path)
        header_request_id = request.headers.get("X-Request-ID")
        request_id = header_request_id or str(uuid.uuid4())
        trace_id = _trace_id(request, request_id)
        trace_token = set_trace_id(trace_id)

        try:
            if route_key not in _REQUEST_ID_REQUIRED_ROUTES:
                response = await call_next(request)
            else:
                if not header_request_id:
                    return _error_response(
                        status_code=422,
                        code=ErrorCode.err_validation,
                        message="参数校验失败",
                        detail="X-Request-ID is required",
                        trace_id=trace_id,
                        request_id=request_id,
                    )
                try:
                    uuid.UUID(header_request_id)
                except ValueError:
                    return _error_response(
                        status_code=422,
                        code=ErrorCode.err_validation,
                        message="参数校验失败",
                        detail="X-Request-ID must be a UUID",
                        trace_id=trace_id,
                        request_id=request_id,
                    )

                if route_key not in _IDEMPOTENT_ROUTES:
                    response = await call_next(request)
                else:
                    response = await self._dispatch_idempotent(
                        request=request,
                        call_next=call_next,
                        route_key=route_key,
                        user_key=self._user_key(request) or "anonymous",
                        request_id=request_id,
                        trace_id=trace_id,
                    )
        finally:
            reset_trace_id(trace_token)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        return response

    async def _dispatch_idempotent(
        self,
        *,
        request: Request,
        call_next,
        route_key: tuple[str, str],
        user_key: str,
        request_id: str,
        trace_id: str,
    ) -> Response:  # type: ignore[no-untyped-def]
        operation_id = f"{route_key[0]}:{route_key[1]}"
        key = idempotency_key(operation_id, user_key, request_id)
        body = await request.body()
        body_hash = canonical_body_hash(body)

        try:
            snapshot, _should_execute = await self._store.begin(key, body_hash)
        except IdempotencyConflictError:
            return _error_response(
                status_code=409,
                code=ErrorCode.err_duplicate_request,
                message="请求重复",
                detail="request id reused with different body",
                trace_id=trace_id,
                request_id=request_id,
            )
        except IdempotencyInProgressError:
            return _error_response(
                status_code=409,
                code=ErrorCode.err_duplicate_request,
                message="请求重复",
                detail="request is still in progress",
                trace_id=trace_id,
                request_id=request_id,
            )
        except (IdempotencyStoreError, RedisError):
            return _error_response(
                status_code=503,
                code=ErrorCode.err_internal,
                message="服务暂不可用",
                detail="idempotency store unavailable",
                trace_id=trace_id,
                request_id=request_id,
            )

        if snapshot is not None:
            return self._replay(snapshot, request_id, trace_id)

        response = await call_next(request)
        raw_body, rebuilt = await _consume_response_body(response)
        if rebuilt.status_code < 500:
            try:
                await self._store.complete(
                    key,
                    snapshot_from_response(
                        body_hash=body_hash,
                        status_code=rebuilt.status_code,
                        body=raw_body,
                        content_type=rebuilt.media_type or "application/json",
                    ),
                )
            except (IdempotencyStoreError, RedisError):
                return _error_response(
                    status_code=503,
                    code=ErrorCode.err_internal,
                    message="服务暂不可用",
                    detail="idempotency store unavailable",
                    trace_id=trace_id,
                    request_id=request_id,
                )
        else:
            try:
                await self._store.release(key)
            except (IdempotencyStoreError, RedisError):
                pass
        return rebuilt

    @staticmethod
    def _replay(
        snapshot: IdempotencySnapshot,
        request_id: str,
        trace_id: str,
    ) -> Response:
        return Response(
            content=response_body_bytes(snapshot),
            status_code=snapshot.status_code,
            media_type=snapshot.content_type,
            headers={"X-Request-ID": request_id, "X-Trace-Id": trace_id},
        )

    @staticmethod
    def _user_key(request: Request) -> str | None:
        authorization = request.headers.get("authorization")
        if not authorization or not authorization.lower().startswith("bearer "):
            return None
        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = JwtService.from_settings().verify_access(token)
        except TokenValidationError:
            return None
        return str(claims.sub)