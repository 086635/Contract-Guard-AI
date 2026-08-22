"""幂等性中间件（X-Request-ID）。

对应 data_scheme.md §4.2：创建 / 提交等写操作，前端 Header 携带
X-Request-ID: <UUID>；后端据此缓存响应，重复提交返回首次结果或
ERR_DUPLICATE_REQUEST。

本中间件负责：
1. 读取 / 生成 X-Request-ID；
2. 透传到响应头（便于前端追踪）；
3. 生成 X-Trace-Id（链路追踪，统一包络 traceId 的同源）；
4. 命中幂等缓存时直接短路（TODO：接 Redis 缓存层）。
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_IDEMPOTENT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        trace_id = request.headers.get("X-Trace-Id") or request_id

        # TODO（工程实现）：对写操作查询 Redis 幂等缓存，命中则直接返回缓存响应。
        # if request.method in _IDEMPOTENT_METHODS and cache.exists(request_id):
        #     return cache.get(request_id)

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        return response
