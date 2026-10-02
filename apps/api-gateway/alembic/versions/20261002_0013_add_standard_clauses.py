"""add standard_clauses

Revision ID: 20261002_0013
Revises: 20261001_0012
Create Date: 2026-10-02

对应：`文件集合/codex/4.C70标准条款库数据与服务.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum as PyEnum

import sqlalchemy as sa
from alembic import op

from rag_contracts.enums import ContractType

revision: str = "20261002_0013"
down_revision: str | Sequence[str] | None = "20261001_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(enum_cls: type[PyEnum], name: str) -> sa.Enum:
    """构造与 ORM 模型一致的枚举约束（native_enum=False → CHECK）。"""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


def upgrade() -> None:
    """新增标准条款库表（人工维护源）。"""
    op.create_table(
        "standard_clauses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "contract_type",
            _enum(ContractType, "standard_clause_contract_type"),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "source",
            sa.String(length=200),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_standard_clauses_type_enabled",
        "standard_clauses",
        ["contract_type", "is_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_standard_clauses_updated_at",
        "standard_clauses",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """回滚标准条款库表。"""
    op.drop_index("ix_standard_clauses_updated_at", table_name="standard_clauses")
    op.drop_index("ix_standard_clauses_type_enabled", table_name="standard_clauses")
    op.drop_table("standard_clauses")
