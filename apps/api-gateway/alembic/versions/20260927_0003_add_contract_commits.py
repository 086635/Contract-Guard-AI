"""add contract version commits

Revision ID: 20260927_0003
Revises: 20260927_0002
Create Date: 2026-09-27

对应：`文件集合/codex/4.C11合同提交与版本边界.md`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260927_0003"
down_revision: str | Sequence[str] | None = "20260927_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加合同版本与提交明细。"""
    op.add_column(
        "contracts",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_table(
        "contract_commits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "request_id",
            "item_id",
            name="uq_contract_commits_request_item",
        ),
    )
    op.create_index(
        "ix_contract_commits_contract_id",
        "contract_commits",
        ["contract_id"],
        unique=False,
    )
    op.create_index(
        "ix_contract_commits_request_id",
        "contract_commits",
        ["request_id"],
        unique=False,
    )


def downgrade() -> None:
    """回滚提交明细与合同版本。"""
    op.drop_index(
        "ix_contract_commits_request_id",
        table_name="contract_commits",
    )
    op.drop_index(
        "ix_contract_commits_contract_id",
        table_name="contract_commits",
    )
    op.drop_table("contract_commits")
    op.drop_column("contracts", "version")
