"""Auth 与 Admin 路由（C47）。

- access token 只从 Authorization Bearer 读取；
- refresh token 只从 HttpOnly `cg_refresh` cookie 读取；
- refresh/logout/admin 写操作校验 Origin；
- 业务规则全部委托 `AuthService`，路由不重复实现鉴权逻辑。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Cookie,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_audit_service,
    get_auth_service,
    get_current_user,
    get_db,
    require_roles,
    verify_origin,
)
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord
from app.generated.schemas import (
    CurrentUser,
    CurrentUserResponse,
    LoginRequest,
    LogoutData,
    LogoutResponse,
    RefreshRequest,
    Role,
    TokenData,
    TokenResponse,
    UserCreateRequest,
    UserListData,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
)
from app.services.audit_service import AuditService
from app.services.auth_service import (
    AccountAlreadyExistsError,
    AccountDisabledError,
    AccountLockedError,
    AccountNotFoundError,
    AuthService,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    LastAdminProtectedError,
    TokenBundle,
)
from app.services.envelope import build_success_response

router = APIRouter(tags=["Auth"])

REFRESH_COOKIE_NAME = "cg_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def _current_user(user: UserRecord) -> CurrentUser:
    """把 C44 安全投影映射为 OpenAPI CurrentUser。"""
    return CurrentUser(
        id=str(user.id),
        username=user.username,
        displayName=user.display_name,
        role=user.role,
        isActive=user.is_active,
        lastLoginAt=user.last_login_at,
        createdAt=user.created_at,
        updatedAt=user.updated_at,
    )


def _token_response(bundle: TokenBundle) -> TokenResponse:
    """把 C46 TokenBundle 映射为 OpenAPI TokenResponse。"""
    return build_success_response(
        TokenResponse,
        TokenData(
            accessToken=bundle.access_token,
            expiresIn=bundle.expires_in,
            user=_current_user(bundle.user),
        ),
    )


def _set_refresh_cookie(response: Response, bundle: TokenBundle) -> None:
    """设置 HttpOnly refresh cookie。"""
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=bundle.refresh_token,
        max_age=bundle.refresh_expires_in,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response) -> None:
    """清除 refresh cookie。"""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )


async def _record_audit(
    audit: AuditService,
    request: Request,
    action: AuditAction,
    outcome: AuditOutcome,
    *,
    actor_id: UUID | None = None,
    actor_role: Role | None = None,
    resource_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """记录认证动作审计；AuditService 内部保证失败降级。"""
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    await audit.write_event(
        action,
        "AUTH",
        outcome,
        actor_id=actor_id,
        actor_role=actor_role,
        resource_id=resource_id,
        metadata=metadata,
        ip_address=ip_address,
        user_agent=user_agent,
    )


def _invalid_credentials(exc: Exception) -> HTTPException:
    """登录/刷新失败统一为 401，不暴露内部原因。"""
    detail = "账号已锁定" if isinstance(exc, AccountLockedError) else "未认证"
    headers: dict[str, str] = {}
    if isinstance(exc, AccountLockedError):
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers=headers or {"WWW-Authenticate": "Bearer"},
    )


def _refresh_token(
    body: RefreshRequest | None,
    cookie_token: str | None,
) -> str:
    """优先使用请求体，其次使用 HttpOnly cookie。"""
    token = body.refresh_token if body is not None else None
    token = token or cookie_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 缺失",
        )
    return token


@router.post(
    "/auth/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """本地账号登录并设置 refresh cookie。"""
    try:
        bundle = await service.login(body.username, body.password.get_secret_value())
    except (InvalidCredentialsError, AccountDisabledError) as exc:
        await _record_audit(audit, request, AuditAction.login, AuditOutcome.failure)
        await db.commit()
        raise _invalid_credentials(exc) from exc
    except AccountLockedError as exc:
        await _record_audit(audit, request, AuditAction.login, AuditOutcome.denied)
        await db.commit()
        raise _invalid_credentials(exc) from exc
    await _record_audit(
        audit,
        request,
        AuditAction.login,
        AuditOutcome.success,
        actor_id=bundle.user.id,
        actor_role=bundle.user.role,
        resource_id=str(bundle.user.id),
        metadata={"method": "POST", "path": "/api/v1/auth/login"},
    )
    await db.commit()
    _set_refresh_cookie(response, bundle)
    return _token_response(bundle)


@router.post(
    "/auth/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_origin)],
)
async def refresh(
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: Annotated[RefreshRequest | None, Body()] = None,
    refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> TokenResponse:
    """轮换 refresh token 并刷新 access token。"""
    refresh_token = _refresh_token(body, refresh_cookie)
    try:
        bundle = await service.refresh(refresh_token)
    except (InvalidRefreshTokenError, AccountDisabledError) as exc:
        await _record_audit(audit, request, AuditAction.refresh, AuditOutcome.failure)
        await db.commit()
        _clear_refresh_cookie(response)
        raise _invalid_credentials(exc) from exc
    await _record_audit(
        audit,
        request,
        AuditAction.refresh,
        AuditOutcome.success,
        actor_id=bundle.user.id,
        actor_role=bundle.user.role,
        resource_id=str(bundle.session_id),
        metadata={"method": "POST", "path": "/api/v1/auth/refresh"},
    )
    await db.commit()
    _set_refresh_cookie(response, bundle)
    return _token_response(bundle)


@router.post(
    "/auth/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_origin)],
)
async def logout(
    request: Request,
    response: Response,
    service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[UserRecord, Depends(get_current_user)],
    refresh_cookie: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> LogoutResponse:
    """撤销当前 refresh 会话并清除 cookie。"""
    if not refresh_cookie:
        await _record_audit(
            audit,
            request,
            AuditAction.logout,
            AuditOutcome.denied,
            actor_id=user.id,
            actor_role=user.role,
        )
        await db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 缺失",
        )
    try:
        await service.logout_by_refresh_token(user.id, refresh_cookie)
    except InvalidRefreshTokenError as exc:
        await _record_audit(
            audit,
            request,
            AuditAction.logout,
            AuditOutcome.denied,
            actor_id=user.id,
            actor_role=user.role,
        )
        await db.commit()
        _clear_refresh_cookie(response)
        raise _invalid_credentials(exc) from exc
    await _record_audit(
        audit,
        request,
        AuditAction.logout,
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(user.id),
        metadata={"method": "POST", "path": "/api/v1/auth/logout"},
    )
    await db.commit()
    _clear_refresh_cookie(response)
    return build_success_response(LogoutResponse, LogoutData(revoked=True))


@router.get(
    "/auth/me",
    response_model=CurrentUserResponse,
    status_code=status.HTTP_200_OK,
)
async def me(
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> CurrentUserResponse:
    """返回当前登录用户。"""
    return build_success_response(CurrentUserResponse, _current_user(user))


@router.get(
    "/admin/users",
    response_model=UserListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_users(
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    service: Annotated[AuthService, Depends(get_auth_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    role: Role | None = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
    query: str | None = None,
) -> UserListResponse:
    """ADMIN 分页查询用户。"""
    del user
    try:
        items, total = await service.list_users(
            page,
            page_size,
            role=role,
            is_active=is_active,
            query=query,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    data = UserListData(
        items=[_current_user(item) for item in items],
        total=total,
        page=page,
        pageSize=page_size,
        hasMore=page * page_size < total,
    )
    return build_success_response(UserListResponse, data)


@router.post(
    "/admin/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_origin)],
)
async def create_user(
    body: UserCreateRequest,
    request: Request,
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserResponse:
    """ADMIN 创建用户。"""
    try:
        created = await service.create_user(
            body.username,
            body.password.get_secret_value(),
            body.role,
            body.display_name,
            is_active=body.is_active is not False,
        )
        await _record_audit(
            audit,
            request,
            AuditAction.user_create,
            AuditOutcome.success,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(created.id),
            metadata={"username": created.username, "role": created.role.value},
        )
        await db.commit()
    except AccountAlreadyExistsError as exc:
        await _record_audit(
            audit,
            request,
            AuditAction.user_create,
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            metadata={"username": body.username},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="用户名已存在",
        ) from exc
    except ValueError as exc:
        await _record_audit(
            audit,
            request,
            AuditAction.user_create,
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            metadata={"username": body.username},
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return build_success_response(UserResponse, _current_user(created))


@router.patch(
    "/admin/users/{user_id}",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_origin)],
)
async def update_user(
    user_id: UUID,
    body: UserUpdateRequest,
    request: Request,
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserResponse:
    """ADMIN 更新用户角色、启用状态或显示名。"""
    try:
        updated = await service.update_user(
            user_id,
            role=body.role,
            is_active=body.is_active,
            display_name=body.display_name,
        )
        await _record_audit(
            audit,
            request,
            AuditAction.user_update,
            AuditOutcome.success,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(updated.id),
            metadata={"role": updated.role.value, "isActive": updated.is_active},
        )
        await db.commit()
    except AccountNotFoundError as exc:
        await _record_audit(
            audit,
            request,
            AuditAction.user_update,
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(user_id),
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户不存在",
        ) from exc
    except LastAdminProtectedError as exc:
        await _record_audit(
            audit,
            request,
            AuditAction.user_update,
            AuditOutcome.denied,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(user_id),
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="不能停用或降级最后一个有效管理员",
        ) from exc
    except ValueError as exc:
        await _record_audit(
            audit,
            request,
            AuditAction.user_update,
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(user_id),
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return build_success_response(UserResponse, _current_user(updated))


__all__ = ["REFRESH_COOKIE_NAME", "REFRESH_COOKIE_PATH", "router"]