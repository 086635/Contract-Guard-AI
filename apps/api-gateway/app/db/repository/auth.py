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
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
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


__all__ = [
    "UserAlreadyExistsError",
    "UserCreate",
    "UserRecord",
    "UserRepository",
    "normalize_username",
]