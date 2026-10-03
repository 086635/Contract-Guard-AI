"""add contract ownership fields

Revision ID: 20261003_0015
Revises: 20261002_0014
Create Date: 2026-10-03

对应：`文件集合/codex/C75-A契约与数据归属收口.md`（P0-1）。
兼容加法：只新增归属列与索引；老合同不回填任何管理员归属。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261003_0015"
down_revision: str | Sequence[str] | None = "20261002_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增 owner/legacy 列，并把无法确定归属的历史合同标记为 legacy。"""
    op.add_column(
        "contracts",
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "contracts",
        sa.Column(
            "legacy",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        "fk_contracts_created_by_id_users",
        "contracts",
        "users",
        ["created_by_id"],
        ["id"],
    )
    op.create_index(
        "ix_contracts_created_by_id",
        "contracts",
        ["created_by_id"],
        unique=False,
    )
    op.execute("UPDATE contracts SET legacy = TRUE WHERE created_by_id IS NULL")


def downgrade() -> None:
    """回滚归属列与索引；不删除任何合同数据。"""
    op.drop_constraint(
        "fk_contracts_created_by_id_users",
        "contracts",
        type_="foreignkey",
    )
    op.drop_index("ix_contracts_created_by_id", table_name="contracts")
    op.drop_column("contracts", "legacy")
    op.drop_column("contracts", "created_by_id")
