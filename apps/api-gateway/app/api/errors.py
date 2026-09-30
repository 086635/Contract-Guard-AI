"""C51 全局异常到 ApiError 的映射。

错误响应保持既有 {code,message,detail,traceId} 字段，并兼容新增 issues。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.generated.schemas import ApiError, ErrorCode, ValidationIssue
from cg_observability import get_trace_id

logger = logging.getLogger("contract_guard.errors")


def _trace_id(request: Request) -> str:
    """读取请求 traceId；无中间件上下文时使用请求头或生成安全占位值。"""
    context_trace = get_trace_id()
    if context_trace:
        return context_trace
    return request.headers.get("X-Trace-Id") or f"tr-{uuid.uuid4().hex[:12]}"


def _error_code(status_code: int, detail: str) -> ErrorCode:
    if status_code in {400, 422}:
        return ErrorCode.err_validation
    if status_code == 401:
        return ErrorCode.err_unauthorized
    if status_code == 403:
        return ErrorCode.err_forbidden
    if status_code == 404:
        return ErrorCode.err_contract_not_found
    if status_code == 409:
        if "duplicate" in detail.lower() or "重复" in detail:
            return ErrorCode.err_duplicate_request
        return ErrorCode.err_data_conflict
    if status_code == 413:
        return ErrorCode.err_file_too_large
    if status_code == 415:
        return ErrorCode.err_unsupported_file_type
    if status_code == 504:
        return ErrorCode.err_llm_timeout
    if status_code == 429:
        return ErrorCode.err_data_conflict
    return ErrorCode.err_internal


def _user_message(status_code: int) -> str:
    return {
        400: "请求参数无效",
        401: "未认证",
        403: "无权限",
        404: "资源不存在",
        409: "数据冲突",
        413: "文件过大",
        415: "文件类型不支持",
        422: "参数校验失败",
        429: "请求过于频繁",
        500: "服务器内部错误",
        502: "上游服务不可用",
        503: "服务暂不可用",
        504: "请求超时",
    }.get(status_code, "请求失败")


def _json_response(
    payload: ApiError,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_headers = dict(headers or {})
    response_headers["X-Trace-Id"] = payload.trace_id
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(by_alias=True, mode="json", exclude_none=True),
        headers=response_headers,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """把 FastAPI HTTPException 映射到统一 ApiError。"""
    trace_id = _trace_id(request)
    detail = exc.detail if isinstance(exc.detail, str) else "request failed"
    payload = ApiError(
        code=_error_code(exc.status_code, detail),
        message=_user_message(exc.status_code),
        detail=detail,
        traceId=trace_id,
    )
    return _json_response(payload, status_code=exc.status_code, headers=exc.headers)


def _issue_path(location: tuple[Any, ...]) -> str:
    """把 FastAPI loc 投影为客户端可用路径。"""
    parts = [str(item) for item in location if item not in {"body", "query", "path", "header", "cookie"}]
    return ".".join(parts)


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """把请求校验错误映射为 ERR_VALIDATION + issues。"""
    trace_id = _trace_id(request)
    issues = [
        ValidationIssue(
            path=_issue_path(tuple(error.get("loc", ()))),
            code=str(error.get("type", "validation_error")),
            message=str(error.get("msg", "invalid value")),
        )
        for error in exc.errors()
    ]
    payload = ApiError(
        code=ErrorCode.err_validation,
        message=_user_message(422),
        detail="request validation failed",
        traceId=trace_id,
        issues=issues,
    )
    return _json_response(payload, status_code=422)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未处理异常只记录服务器日志，不向客户端暴露堆栈。"""
    trace_id = _trace_id(request)
    logger.exception("unhandled exception trace_id=%s", trace_id)
    payload = ApiError(
        code=ErrorCode.err_internal,
        message=_user_message(500),
        detail="internal server error",
        traceId=trace_id,
    )
    return _json_response(payload, status_code=500)


def register_error_handlers(app: FastAPI) -> None:
    """注册全局错误处理器。"""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "http_exception_handler",
    "register_error_handlers",
    "unhandled_exception_handler",
    "validation_exception_handler",
]