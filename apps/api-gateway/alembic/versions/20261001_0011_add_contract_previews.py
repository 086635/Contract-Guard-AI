"""add contract previews

Revision ID: 20261001_0011
Revises: 20260930_0010
Create Date: 2026-10-01

对应：`文件集合/codex/4.C58预览元数据与预览API.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum as PyEnum

import sqlalchemy as sa
from alembic import op

from app.generated.schemas import PreviewStatus

revision: str = "20261001_0011"
down_revision: str | Sequence[str] | None = "20260930_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(enum_cls: type[PyEnum], name: str) -> sa.Enum:
    """构造与 ORM 模型一致的枚举约束。"""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


def upgrade() -> None:
    """新增预览元数据表（一合同一行）。"""
    op.create_table(
        "contract_previews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("status", _enum(PreviewStatus, "preview_status"), nullable=False),
        sa.Column("preview_object_key", sa.String(length=1024), nullable=True),
        sa.Column("convert_task_id", sa.String(length=255), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("converter_version", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_id", name="uq_contract_previews_contract"),
    )
    op.create_index(
        "ix_contract_previews_status",
        "contract_previews",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    """回滚预览元数据表。"""
    op.drop_index("ix_contract_previews_status", table_name="contract_previews")
    op.drop_table("contract_previews")
