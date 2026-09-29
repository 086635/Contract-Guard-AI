"""add settings review thresholds

Revision ID: 20260929_0006
Revises: 20260929_0005
Create Date: 2026-09-29

对应：`文件集合/codex/4.C39设置契约与阈值持久化.md`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0006"
down_revision: str | Sequence[str] | None = "20260929_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_THRESHOLDS = (
    '{"high": 0.85, "medium": 0.65, "low": 0.40}'
)


def upgrade() -> None:
    """增加设置阈值 JSON 列并回填默认值。"""
    op.add_column(
        "settings_rules",
        sa.Column(
            "review_thresholds",
            sa.JSON(),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT_THRESHOLDS}'"),
        ),
    )


def downgrade() -> None:
    """回滚设置阈值列。"""
    op.drop_column("settings_rules", "review_thresholds")
