"""add local users

Revision ID: 20260929_0007
Revises: 20260929_0006
Create Date: 2026-09-29

对应：`文件集合/codex/4.C44认证契约与账号数据模型.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum as PyEnum

import sqlalchemy as sa
from alembic import op

from app.generated.schemas import Role

revision: str = "20260929_0007"
down_revision: str | Sequence[str] | None = "20260929_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(enum_cls: type[PyEnum], name: str) -> sa.Enum:
    """构造与 ORM 模型一致的用户角色约束。"""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


def upgrade() -> None:
    """新增 users 表；不创建默认账号。"""
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", _enum(Role, "user_role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )
    op.create_index("ix_users_role", "users", ["role"], unique=False)
    op.create_index("ix_users_is_active", "users", ["is_active"], unique=False)


def downgrade() -> None:
    """回滚 users 表。"""
    op.drop_index("ix_users_is_active", table_name="users")
    op.drop_index("ix_users_role", table_name="users")
    op.drop_table("users")