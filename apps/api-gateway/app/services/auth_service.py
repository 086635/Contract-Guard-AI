"""C46 认证与账号策略服务。

依赖方向固定为：

```text
AuthService -> UserRepository / SessionRepository / security primitives
```

本模块不依赖 FastAPI Request/Response，不直接写 SQL；所有状态变更只 flush，
由调用方（后续 C47 路由）在请求事务中统一提交。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repository.auth import (
    RefreshTokenReusedError,
    SessionRepository,
    SessionRepositoryError,
    UserAlreadyExistsError,
    UserCreate,
    UserRecord,
    UserRepository,
)
from app.generated.schemas import Role
from app.services.security import (
    JwtService,
    PasswordService,
    RefreshTokenError,
    generate_refresh_token,
    hash_refresh_token,
)

# 固定的假哈希仅用于用户不存在时做等成本密码校验，绝不是可用凭据。
_DUMMY_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$mvvVC+vGp76A2ZzOChbpPg$dIsTCc5e/mchvFYBskV22q30iqsVB4HX0PSLVqigCpc"


class AuthServiceError(ValueError):
    """认证与账号策略领域异常基类。"""


class InvalidCredentialsError(AuthServiceError):
    """用户名或密码错误；不区分具体失败原因。"""


class AccountLockedError(AuthServiceError):
    """账号因连续登录失败被锁定。"""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, retry_after_seconds)


class InvalidRefreshTokenError(AuthServiceError):
    """refresh token 不存在、过期、撤销或重放。"""


class AccountDisabledError(AuthServiceError):
    """账号已停用。"""


class AccountNotFoundError(AuthServiceError):
    """账号不存在。"""


class AccountAlreadyExistsError(AuthServiceError):
    """用户名已存在。"""


class LastAdminProtectedError(AuthServiceError):
    """不能停用或降级最后一个有效管理员。"""


class TokenBundle(BaseModel):
    """登录/刷新成功后的内部令牌包；由 C47 映射为 HTTP 契约。"""

    access_token: str
    refresh_token: str
    expires_in: int
    refresh_expires_in: int
    user: UserRecord
    session_id: UUID


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """把时间转换为 UTC，拒绝 naive datetime。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


class AuthService:
    """认证用例与账号策略的唯一实现处。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        jwt_service: JwtService | None = None,
        password_service: PasswordService | None = None,
        now: Callable[[], datetime] | None = None,
        lockout_threshold: int | None = None,
        lockout_minutes: int | None = None,
        refresh_ttl_seconds: int | None = None,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._sessions = SessionRepository(session)
        self._jwt = jwt_service or JwtService.from_settings()
        self._passwords = password_service or PasswordService()
        self._now = now or _utc_now
        self._lockout_threshold = (
            lockout_threshold
            if lockout_threshold is not None
            else settings.auth_lockout_threshold
        )
        self._lockout_minutes = (
            lockout_minutes if lockout_minutes is not None else settings.auth_lockout_minutes
        )
        self._refresh_ttl_seconds = (
            refresh_ttl_seconds
            if refresh_ttl_seconds is not None
            else settings.jwt_refresh_ttl_seconds
        )
        if self._lockout_threshold < 1:
            raise ValueError("lockout_threshold 必须大于等于 1")
        if self._lockout_minutes < 1:
            raise ValueError("lockout_minutes 必须大于等于 1")
        if self._refresh_ttl_seconds < 1:
            raise ValueError("refresh_ttl_seconds 必须大于等于 1")

    async def login(self, username: str, password: str) -> TokenBundle:
        """执行本地账号登录，并应用持久化失败锁定策略。"""
        now = _as_utc(self._now())
        user = await self._users.get_auth_by_username(username)

        if user is None:
            self._passwords.verify(password, _DUMMY_PASSWORD_HASH)
            raise InvalidCredentialsError("用户名或密码错误")

        if not user.is_active:
            self._passwords.verify(password, user.password_hash)
            raise InvalidCredentialsError("用户名或密码错误")

        locked_until = _as_utc(user.locked_until) if user.locked_until else None
        if locked_until is not None and locked_until > now:
            self._passwords.verify(password, user.password_hash)
            retry_after = int((locked_until - now).total_seconds())
            raise AccountLockedError("账号已锁定", retry_after_seconds=retry_after)

        attempts = user.failed_login_attempts
        if locked_until is not None and locked_until <= now:
            attempts = 0

        if not self._passwords.verify(password, user.password_hash):
            next_attempts = attempts + 1
            locked_until = (
                now + timedelta(minutes=self._lockout_minutes)
                if next_attempts >= self._lockout_threshold
                else None
            )
            await self._users.record_login_failure(
                user.id,
                failed_login_attempts=next_attempts,
                locked_until=locked_until,
                now=now,
            )
            if locked_until is not None:
                raise AccountLockedError(
                    "账号已锁定",
                    retry_after_seconds=int(
                        (locked_until - now).total_seconds()
                    ),
                )
            raise InvalidCredentialsError("用户名或密码错误")

        updated_user = await self._users.mark_login_success(user.id, now=now)
        return await self._issue_bundle(updated_user, now)

    async def refresh(self, refresh_token: str) -> TokenBundle:
        """轮换 refresh token 并签发新的 access token。"""
        now = _as_utc(self._now())
        try:
            old_hash = hash_refresh_token(refresh_token)
        except RefreshTokenError as exc:
            raise InvalidRefreshTokenError("refresh token 无效") from exc
        existing = await self._sessions.get_by_hash(old_hash)
        if existing is None:
            raise InvalidRefreshTokenError("refresh token 无效")

        user = await self._users.get(existing.user_id)
        if user is None or not user.is_active:
            await self._sessions.revoke(existing.id, now=now)
            raise AccountDisabledError("账号不可用")

        new_refresh_token = generate_refresh_token()
        new_hash = hash_refresh_token(new_refresh_token)
        try:
            rotated = await self._sessions.rotate(
                old_hash,
                new_hash,
                now + timedelta(seconds=self._refresh_ttl_seconds),
                now=now,
            )
        except RefreshTokenReusedError as exc:
            # 重放检测已在同一 session 中撤销全部活动会话；
            # 这是必须持久化的安全补偿动作，不能随失败响应回滚。
            await self._session.commit()
            raise InvalidRefreshTokenError("refresh token 无效") from exc
        except SessionRepositoryError as exc:
            raise InvalidRefreshTokenError("refresh token 无效") from exc

        return TokenBundle(
            access_token=self._jwt.issue_access(user),
            refresh_token=new_refresh_token,
            expires_in=self._jwt.access_ttl_seconds,
            refresh_expires_in=self._refresh_ttl_seconds,
            user=user,
            session_id=rotated.id,
        )

    async def logout(self, user_id: UUID, session_id: UUID) -> None:
        """撤销当前用户自己的 session；不存在的 session 按无效处理。"""
        session = await self._sessions.get(session_id)
        if session is None or session.user_id != user_id:
            raise InvalidRefreshTokenError("refresh session 无效")
        await self._sessions.revoke(session_id, now=_as_utc(self._now()))

    async def me(self, user_id: UUID) -> UserRecord:
        """返回当前有效用户。"""
        user = await self._users.get(user_id)
        if user is None:
            raise AccountNotFoundError("账号不存在")
        if not user.is_active:
            raise AccountDisabledError("账号已停用")
        return user

    async def create_user(
        self,
        username: str,
        password: str,
        role: Role,
        display_name: str,
        *,
        is_active: bool = True,
    ) -> UserRecord:
        """创建用户；密码只以 Argon2id 哈希进入仓储。"""
        password_hash = self._passwords.hash(password)
        try:
            return await self._users.create(
                UserCreate(
                    username=username,
                    password_hash=password_hash,
                    role=Role(role),
                    display_name=display_name,
                    is_active=is_active,
                )
            )
        except UserAlreadyExistsError as exc:
            raise AccountAlreadyExistsError("用户名已存在") from exc

    async def update_user(
        self,
        user_id: UUID,
        *,
        role: Role | None = None,
        is_active: bool | None = None,
        display_name: str | None = None,
    ) -> UserRecord:
        """更新账号；最后一个有效管理员不可被停用或降级。"""
        current = await self._users.get(user_id)
        if current is None:
            raise AccountNotFoundError("账号不存在")

        next_role = Role(role) if role is not None else current.role
        next_active = current.is_active if is_active is None else is_active
        removes_active_admin = (
            current.role == Role.admin
            and current.is_active
            and (next_role != Role.admin or not next_active)
        )
        if removes_active_admin:
            active_admins = await self._users.list_active_admins_for_update()
            if len(active_admins) <= 1:
                raise LastAdminProtectedError("不能停用或降级最后一个有效管理员")

        updated = await self._users.update(
            user_id,
            role=next_role if role is not None else None,
            is_active=is_active,
            display_name=display_name,
        )

        role_changed = next_role != current.role
        if not next_active or role_changed:
            await self._sessions.revoke_all_for_user(
                user_id,
                now=_as_utc(self._now()),
            )
        return updated

    async def _issue_bundle(
        self,
        user: UserRecord,
        now: datetime,
    ) -> TokenBundle:
        """签发 access 并创建新的 refresh session。"""
        refresh_token = generate_refresh_token()
        refresh_hash = hash_refresh_token(refresh_token)
        session = await self._sessions.create(
            user.id,
            refresh_hash,
            now + timedelta(seconds=self._refresh_ttl_seconds),
        )
        return TokenBundle(
            access_token=self._jwt.issue_access(user),
            refresh_token=refresh_token,
            expires_in=self._jwt.access_ttl_seconds,
            refresh_expires_in=self._refresh_ttl_seconds,
            user=user,
            session_id=session.id,
        )


__all__ = [
    "AccountAlreadyExistsError",
    "AccountDisabledError",
    "AccountLockedError",
    "AccountNotFoundError",
    "AuthService",
    "AuthServiceError",
    "InvalidCredentialsError",
    "InvalidRefreshTokenError",
    "LastAdminProtectedError",
    "TokenBundle",
]