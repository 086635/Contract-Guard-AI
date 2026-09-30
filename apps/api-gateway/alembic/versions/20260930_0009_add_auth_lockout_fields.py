"""add auth lockout fields

Revision ID: 20260930_0009
Revises: 20260929_0008
Create Date: 2026-09-30

对应：`文件集合/codex/4.C46AuthService与账号策略.md`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0009"
down_revision: str | Sequence[str] | None = "20260929_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加持久化登录失败计数与锁定时间。"""
    op.add_column(
        "users",
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """回滚登录锁定字段。"""
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_attempts")