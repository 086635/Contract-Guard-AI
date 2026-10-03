"""Tasks 路由（长耗时异步任务进度订阅，§5.4）。

C64：SSE 支持 `?streamToken=` 短期令牌（EventSource 无法携带 Authorization 头），
同时保留 `Authorization: Bearer` 兼容路径；两条路径的鉴权都在 `stream_principal` 里，
**不新增第二套 SSE 通道**，也不放宽匿名订阅。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_service_context
from app.db.repository.auth import UserRecord, UserRepository
from app.generated.schemas import StreamTokenResponse, TaskProgressResponse
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.security import TokenValidationError
from app.services.sse import SSE_HEADERS, SSE_MEDIA_TYPE
from app.services.task_service import (
    StreamTokenScopeError,
    TaskNotFoundError,
    TaskService,
)

router = APIRouter(prefix="/tasks", tags=["Tasks"])

#: SSE 凭据解析器（`auto_error=False`：没有 Authorization 时交给 streamToken 分支）。
_stream_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    """统一的 401（带 WWW-Authenticate，保持与 `get_current_user` 同形）。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _assert_task_exists(
    service: TaskService,
    task_id: UUID,
    user: UserRecord | None,
) -> None:
    """任务不存在或越权 → 404（不泄露存在性）。"""
    try:
        await service.assert_access(task_id, user)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        ) from exc


async def stream_principal(
    task_id: UUID,
    context: Annotated[ServiceContext, Depends(get_service_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_stream_scheme),
    ],
    stream_token: Annotated[str | None, Query(alias="streamToken")] = None,
) -> UserRecord | None:
    """SSE 订阅凭据（**唯一处**）。

    ① 带 `Authorization: Bearer <access>` → 复用 `get_current_user`（兼容路径）；
    ② 否则必须带 `?streamToken=<短期令牌>`（EventSource 路径）。

    🔴 access token **放查询串**不会被接受：`verify_stream` 要求 `typ=stream` 且含
    `taskId`/`scope`，access token 两者都不满足（断言 4）。
    """
    service = TaskService(context)
    if credentials is not None:
        user = await get_current_user(credentials, db)
        await _assert_task_exists(service, task_id, user)
        return user
    if not stream_token:
        raise _unauthorized("未认证")
    try:
        claims = service.verify_stream_token(task_id, stream_token)
    except TokenValidationError as exc:
        raise _unauthorized("streamToken 无效或已过期") from exc
    except StreamTokenScopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="streamToken 不属于该任务",
        ) from exc
    user = await UserRepository(db).get(claims.sub)
    if user is None or not user.is_active:
        raise _unauthorized("未认证")
    await _assert_task_exists(service, task_id, user)
    return user


@router.get("/{task_id}", response_model=TaskProgressResponse, status_code=200)
async def get_task(
    task_id: UUID,
    user: Annotated[UserRecord, Depends(get_current_user)],
    context: ServiceContext = Depends(get_service_context),
) -> TaskProgressResponse:
    """长耗时任务进度轮询。"""
    service = TaskService(context)
    await _assert_task_exists(service, task_id, user)
    progress = await service.get(task_id)
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        )
    return build_success_response(TaskProgressResponse, progress)


@router.post(
    "/{task_id}/stream-token",
    response_model=StreamTokenResponse,
    status_code=status.HTTP_200_OK,
)
async def create_task_stream_token(
    task_id: UUID,
    user: Annotated[UserRecord, Depends(get_current_user)],
    context: ServiceContext = Depends(get_service_context),
) -> StreamTokenResponse:
    """签发 SSE 订阅用的短期 stream token（60 秒，绑定 userId + taskId）。"""
    try:
        data = await TaskService(context).issue_stream_token(task_id, user)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        ) from exc
    return build_success_response(StreamTokenResponse, data)


@router.get("/{task_id}/stream")
async def stream_task(
    task_id: UUID,
    _principal: Annotated[UserRecord | None, Depends(stream_principal)],
    context: ServiceContext = Depends(get_service_context),
) -> StreamingResponse:
    """长耗时任务进度 SSE 订阅（`text/event-stream`；凭据见 `stream_principal`）。"""
    service = TaskService(context)
    try:
        await service.get(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        ) from exc
    return StreamingResponse(
        service.stream(task_id),
        media_type=SSE_MEDIA_TYPE,
        headers=dict(SSE_HEADERS),
    )
