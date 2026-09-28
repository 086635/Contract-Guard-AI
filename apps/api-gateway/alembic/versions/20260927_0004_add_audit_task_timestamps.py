"""add audit task timestamps

Revision ID: 20260927_0004
Revises: 20260927_0003
Create Date: 2026-09-27

对应：`文件集合/codex/4.C13合同结果查询服务.md`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0004"
down_revision: str | Sequence[str] | None = "20260927_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加任务时间戳，支持 latest task 查询。"""
    op.add_column(
        "audit_tasks",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "audit_tasks",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    """回滚任务时间戳。"""
    op.drop_column("audit_tasks", "updated_at")
    op.drop_column("audit_tasks", "created_at")
