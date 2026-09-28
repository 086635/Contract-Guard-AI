"""add seal detections

Revision ID: 20260927_0002
Revises: 20260926_0001
Create Date: 2026-09-27

对应：`文件集合/codex/4.C5结果DAL.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum as PyEnum

import sqlalchemy as sa
from alembic import op

from app.generated.schemas import SealStatus, SealType

# revision identifiers, used by Alembic.
revision: str = "20260927_0002"
down_revision: str | Sequence[str] | None = "20260926_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(enum_cls: type[PyEnum], name: str) -> sa.Enum:
    """构造与 ORM 模型一致的契约枚举列。"""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


def upgrade() -> None:
    """创建印章与签名检测结果表。"""
    op.create_table(
        "seal_detections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            _enum(SealType, "seal_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum(SealStatus, "seal_status"),
            nullable=False,
        ),
        sa.Column("bbox", sa.JSON(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("is_matched_with_parties", sa.Boolean(), nullable=False),
        sa.Column("compliance_note", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.CheckConstraint("page >= 1", name="ck_seal_detections_page"),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_seal_detections_confidence",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_seal_detections_contract_id",
        "seal_detections",
        ["contract_id"],
        unique=False,
    )
    op.create_index(
        "ix_seal_detections_type_status",
        "seal_detections",
        ["type", "status"],
        unique=False,
    )


def downgrade() -> None:
    """回滚印章与签名检测结果表。"""
    op.drop_index(
        "ix_seal_detections_type_status",
        table_name="seal_detections",
    )
    op.drop_index(
        "ix_seal_detections_contract_id",
        table_name="seal_detections",
    )
    op.drop_table("seal_detections")
