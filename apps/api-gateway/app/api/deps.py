"""FastAPI Service 层依赖注入。

设计要点：
1. `get_db()` 复用 C3 的唯一 session 生成器。
2. 具体外部适配器延迟到 C8，并允许测试通过 dependency override 替换。
3. 路由只依赖 `get_service_context()`，不自行构造外部客户端。

对应：`文件集合/codex/4.C7服务基座与依赖注入.md`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord, UserRepository
from app.db.session import get_session
from app.generated.schemas import Role
from app.services.audit_service import AuditService
from app.services.auth_service import AuthService
from app.services.context import ServiceContext
from app.services.ports import (
    ObjectStoragePort,
    ProgressBrokerPort,
    TaskDispatcherPort,
)
from app.services.security import JwtService, TokenValidationError
from app.services.standard_clause_service import StandardClauseService


async def get_db() -> AsyncIterator[AsyncSession]:
    """提供单个请求的数据库会话。"""
    async for session in get_session():
        yield session


def get_object_storage() -> ObjectStoragePort:
    """延迟构造对象存储适配器。"""
    from app.services.storage import MinioObjectStorage

    return MinioObjectStorage()


def get_task_dispatcher() -> TaskDispatcherPort:
    """延迟构造任务派发适配器。"""
    from app.services.dispatch import CeleryTaskDispatcher

    return CeleryTaskDispatcher()


def get_progress_broker() -> ProgressBrokerPort:
    """延迟构造进度订阅适配器。"""
    from app.services.progress import RedisProgressBroker

    return RedisProgressBroker()


async def get_service_context(
    db: Annotated[AsyncSession, Depends(get_db)],
    object_storage: Annotated[ObjectStoragePort, Depends(get_object_storage)],
    task_dispatcher: Annotated[TaskDispatcherPort, Depends(get_task_dispatcher)],
    progress_broker: Annotated[ProgressBrokerPort, Depends(get_progress_broker)],
) -> ServiceContext:
    """组装单请求 Service 上下文。"""
    return ServiceContext(
        session=db,
        object_storage=object_storage,
        task_dispatcher=task_dispatcher,
        progress_broker=progress_broker,
    )


async def get_auth_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthService:
    """为 auth/admin 路由组装 AuthService。"""
    return AuthService(db)


async def get_audit_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuditService:
    """为路由组装审计服务；写失败由 AuditService 自行告警降级。"""
    return AuditService(db)


async def get_standard_clause_service(
    context: Annotated[ServiceContext, Depends(get_service_context)],
) -> StandardClauseService:
    """为标准条款库路由组装服务（PG 维护源 + RAG 索引写入）。

    走 `get_service_context()` 取同一个请求会话：既满足「路由只依赖 ServiceContext」
    的守卫（C17），也保证元数据事务与审计写入共用一条 session。
    """
    return StandardClauseService(context.session)


_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserRecord:
    """从 Authorization Bearer 解析并加载当前有效用户。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = JwtService.from_settings().verify_access(credentials.credentials)
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    user = await UserRepository(db).get(claims.sub)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_roles(*roles: Role):
    """构造角色门槛依赖；单一实现处，不在各路由重复判断。"""
    required = set(roles)

    async def _dependency(
        request: Request,
        user: Annotated[UserRecord, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
        audit: Annotated[AuditService, Depends(get_audit_service)],
    ) -> UserRecord:
        if user.role not in required:
            await audit.write_event(
                AuditAction.auth_denied,
                "AUTH",
                AuditOutcome.denied,
                actor_id=user.id,
                actor_role=user.role,
                resource_id=request.url.path,
                metadata={"method": request.method, "path": request.url.path},
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权限",
            )
        return user

    return _dependency


async def verify_origin(
    origin: str | None = Header(default=None, alias="Origin"),
) -> None:
    """对所有 cookie/管理写操作校验 Origin；拒绝跨站请求。"""
    if origin not in settings.cors_origins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="origin not allowed",
        )


async def write_audit_event(
    service: AuditService,
    request: Request,
    action: AuditAction,
    resource_type: str,
    outcome: AuditOutcome,
    *,
    actor_id: object | None = None,
    actor_role: Role | None = None,
    resource_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """从 HTTP 上下文补全 IP/UA 后写入审计。"""
    await service.write_event(
        action,
        resource_type,
        outcome,
        actor_id=actor_id,
        actor_role=actor_role,
        resource_id=resource_id,
        metadata=metadata,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


__all__ = [
    "get_audit_service",
    "get_auth_service",
    "get_current_user",
    "get_db",
    "get_object_storage",
    "get_progress_broker",
    "get_service_context",
    "get_standard_clause_service",
    "get_task_dispatcher",
    "require_roles",
    "verify_origin",
    "write_audit_event",
]
