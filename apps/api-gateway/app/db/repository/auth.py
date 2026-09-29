"""C44 本地用户仓储。

设计要点：
1. 仓储只依赖 SQLAlchemy session 与 `User` 模型。
2. 用户名以规范化值持久化并依赖唯一约束防重复。
3. 返回表级 `UserRecord`，不向路由层泄漏 ORM 实例或密码哈希。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthSession, User
from app.generated.schemas import Role


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class UserCreate(_PersistenceDto):
    """创建用户所需持久化字段。"""

    username: str = Field(min_length=1)
    password_hash: str = Field(min_length=1)
    role: Role
    is_active: bool = True
    display_name: str = Field(min_length=1)
    id: UUID | None = None


class UserRecord(_PersistenceDto):
    """用户表安全投影，不包含密码哈希。"""

    id: UUID
    username: str
    role: Role
    is_active: bool
    display_name: str
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserAlreadyExistsError(ValueError):
    """用户名已经存在。"""


def normalize_username(value: str) -> str:
    """规范化用户名，确保大小写和首尾空白不会产生重复账号。"""
    normalized = value.strip().lower()
    if not normalized:
        raise ValueError("username 不能为空")
    return normalized


class UserRepository:
    """用户表仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, user: UserCreate) -> UserRecord:
        """创建用户并 flush；重复用户名明确失败。"""
        record = User(
            id=user.id or uuid4(),
            username=normalize_username(user.username),
            password_hash=user.password_hash,
            role=Role(user.role),
            is_active=user.is_active,
            display_name=user.display_name.strip(),
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise UserAlreadyExistsError(f"username already exists: {record.username}") from exc
        return UserRecord.model_validate(record)

    async def get(self, user_id: UUID) -> UserRecord | None:
        """按主键读取安全用户投影。"""
        record = await self._session.get(User, user_id)
        if record is None:
            return None
        return UserRecord.model_validate(record)

    async def get_by_username(self, username: str) -> UserRecord | None:
        """按规范化用户名读取安全用户投影。"""
        statement = select(User).where(User.username == normalize_username(username))
        record = await self._session.scalar(statement)
        if record is None:
            return None
        return UserRecord.model_validate(record)

    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        role: Role | None = None,
        is_active: bool | None = None,
        query: str | None = None,
    ) -> tuple[list[UserRecord], int]:
        """分页查询用户，支持角色、启用状态和用户名/显示名筛选。"""
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size 必须在 1..100 之间")

        filters = []
        if role is not None:
            filters.append(User.role == Role(role))
        if is_active is not None:
            filters.append(User.is_active == is_active)
        if query and query.strip():
            keyword = f"%{normalize_username(query)}%"
            filters.append(or_(User.username.ilike(keyword), User.display_name.ilike(keyword)))

        count_statement = select(func.count()).select_from(User)
        statement = (
            select(User)
            .order_by(User.username.asc(), User.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        if filters:
            count_statement = count_statement.where(*filters)
            statement = statement.where(*filters)

        total = await self._session.scalar(count_statement)
        records = (await self._session.scalars(statement)).all()
        return [UserRecord.model_validate(record) for record in records], int(total or 0)

    async def update(
        self,
        user_id: UUID,
        *,
        role: Role | None = None,
        is_active: bool | None = None,
        display_name: str | None = None,
    ) -> UserRecord:
        """更新用户角色、启用状态或显示名。"""
        record = await self._session.get(User, user_id)
        if record is None:
            raise LookupError(f"用户不存在: {user_id}")
        if role is not None:
            record.role = Role(role)
        if is_active is not None:
            record.is_active = is_active
        if display_name is not None:
            normalized_display_name = display_name.strip()
            if not normalized_display_name:
                raise ValueError("display_name 不能为空")
            record.display_name = normalized_display_name
        record.updated_at = datetime.now(UTC)
        await self._session.flush()
        return UserRecord.model_validate(record)

class SessionRepositoryError(ValueError):
    """会话持久化领域错误。"""


class SessionNotFoundError(SessionRepositoryError):
    """refresh 会话不存在。"""


class SessionExpiredError(SessionRepositoryError):
    """refresh 会话已过期。"""


class SessionRevokedError(SessionRepositoryError):
    """refresh 会话已撤销。"""


class RefreshTokenReusedError(SessionRevokedError):
    """已轮换或已登出的 refresh token 被重放。"""


def _as_utc(value: datetime) -> datetime:
    """把时间统一为 UTC，拒绝无时区输入。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


class SessionRecord(_PersistenceDto):
    """refresh 会话安全投影；不包含 refresh token 哈希。"""

    id: UUID
    user_id: UUID
    expires_at: datetime
    revoked_at: datetime | None
    replaced_by_id: UUID | None
    created_at: datetime
    last_used_at: datetime | None


class SessionRepository:
    """refresh 会话仓储；轮换、撤销和重放检测的唯一实现处。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user_id: UUID,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> SessionRecord:
        """创建 refresh 会话并 flush；调用方负责事务提交。"""
        record = AuthSession(
            id=uuid4(),
            user_id=user_id,
            refresh_token_hash=refresh_token_hash.strip().lower(),
            expires_at=_as_utc(expires_at),
        )
        self._session.add(record)
        await self._session.flush()
        return SessionRecord.model_validate(record)

    async def get_by_hash(self, refresh_token_hash: str) -> SessionRecord | None:
        """按 refresh token 哈希读取会话；保留撤销记录用于重放检测。"""
        statement = select(AuthSession).where(
            AuthSession.refresh_token_hash == refresh_token_hash.strip().lower()
        )
        record = await self._session.scalar(statement)
        if record is None:
            return None
        return SessionRecord.model_validate(record)

    async def rotate(
        self,
        refresh_token_hash: str,
        new_refresh_token_hash: str,
        new_expires_at: datetime,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        """单次轮换 refresh token；旧 token 重放会撤销该用户全部活动会话。"""
        current_time = _as_utc(now or datetime.now(UTC))
        statement = (
            select(AuthSession)
            .where(
                AuthSession.refresh_token_hash == refresh_token_hash.strip().lower()
            )
            .with_for_update()
        )
        current = await self._session.scalar(statement)
        if current is None:
            raise SessionNotFoundError("refresh session not found")

        if current.revoked_at is not None or current.replaced_by_id is not None:
            await self._revoke_all_for_user(current.user_id, current_time)
            raise RefreshTokenReusedError("refresh token reused")

        if current.expires_at <= current_time:
            current.revoked_at = current_time
            await self._session.flush()
            raise SessionExpiredError("refresh session expired")

        new_session = AuthSession(
            id=uuid4(),
            user_id=current.user_id,
            refresh_token_hash=new_refresh_token_hash.strip().lower(),
            expires_at=_as_utc(new_expires_at),
            created_at=current_time,
        )
        self._session.add(new_session)
        await self._session.flush()
        current.revoked_at = current_time
        current.replaced_by_id = new_session.id
        current.last_used_at = current_time
        await self._session.flush()
        return SessionRecord.model_validate(new_session)

    async def revoke(
        self,
        session_id: UUID,
        *,
        now: datetime | None = None,
    ) -> bool:
        """撤销指定会话；重复撤销保持幂等。"""
        record = await self._session.get(AuthSession, session_id)
        if record is None:
            return False
        if record.revoked_at is None:
            record.revoked_at = _as_utc(now or datetime.now(UTC))
            await self._session.flush()
        return True

    async def revoke_by_hash(
        self,
        refresh_token_hash: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """按 refresh token 哈希撤销会话；不存在时返回 False。"""
        statement = select(AuthSession).where(
            AuthSession.refresh_token_hash == refresh_token_hash.strip().lower()
        )
        record = await self._session.scalar(statement)
        if record is None:
            return False
        return await self.revoke(record.id, now=now)

    async def _revoke_all_for_user(self, user_id: UUID, now: datetime) -> None:
        """重放检测命中时撤销该用户全部未撤销会话。"""
        await self._session.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

__all__ = [
    "RefreshTokenReusedError",
    "SessionExpiredError",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionRepository",
    "SessionRepositoryError",
    "SessionRevokedError",
    "UserAlreadyExistsError",
    "UserCreate",
    "UserRecord",
    "UserRepository",
    "normalize_username",
]