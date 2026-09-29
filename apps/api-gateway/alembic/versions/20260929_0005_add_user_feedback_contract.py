"""add user feedback contract

Revision ID: 20260929_0005
Revises: 20260927_0004
Create Date: 2026-09-29

对应：`文件集合/codex/4.C38反馈列表契约与持久化.md`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260929_0005"
down_revision: str | Sequence[str] | None = "20260927_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加反馈合同关联与 reviewer，并安全回填旧 RISK/CONFLICT 数据。"""
    op.add_column(
        "user_feedbacks",
        sa.Column("contract_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "user_feedbacks",
        sa.Column("reviewer", sa.String(length=255), nullable=True),
    )
    if not context.is_offline_mode():
        op.create_foreign_key(
            "fk_user_feedbacks_contract_id",
            "user_feedbacks",
            "contracts",
            ["contract_id"],
            ["id"],
        )
    op.create_index(
        "ix_user_feedbacks_contract_created",
        "user_feedbacks",
        ["contract_id", "created_at"],
        unique=False,
    )

    if not context.is_offline_mode():
        bind = op.get_bind()
        bind.execute(
            sa.text(
                """
                UPDATE user_feedbacks AS f
                SET contract_id = r.contract_id
                FROM audit_risks AS r
                WHERE f.contract_id IS NULL
                  AND f.target_type IN ('RISK', 'CONFLICT')
                  AND f.target_id = r.id
                """
            )
        )
        remaining = bind.execute(
            sa.text(
                "SELECT count(*) FROM user_feedbacks WHERE contract_id IS NULL"
            )
        ).scalar_one()
        if remaining:
            raise RuntimeError(
                f"{remaining} 条旧反馈无法回填 contract_id，拒绝静默迁移"
            )
    if not context.is_offline_mode():
        op.alter_column(
            "user_feedbacks",
            "contract_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )


def downgrade() -> None:
    """回滚反馈合同关联与 reviewer。"""
    op.drop_index(
        "ix_user_feedbacks_contract_created",
        table_name="user_feedbacks",
    )
    if not context.is_offline_mode():
        op.drop_constraint(
            "fk_user_feedbacks_contract_id",
            "user_feedbacks",
            type_="foreignkey",
        )
    op.drop_column("user_feedbacks", "reviewer")
    op.drop_column("user_feedbacks", "contract_id")
