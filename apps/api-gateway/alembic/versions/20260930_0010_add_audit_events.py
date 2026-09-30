"""add audit events

Revision ID: 20260930_0010
Revises: 20260930_0009
Create Date: 2026-09-30

对应：`文件集合/codex/4.C49审计事件表与仓储.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum as PyEnum

import sqlalchemy as sa
from alembic import op

from app.db.models import AuditAction, AuditOutcome
from app.generated.schemas import Role

revision: str = "20260930_0010"
down_revision: str | Sequence[str] | None = "20260930_0009"
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
    """新增 append-only 审计事件表。"""
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_role", _enum(Role, "audit_actor_role"), nullable=True),
        sa.Column("action", _enum(AuditAction, "audit_action"), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", _enum(AuditOutcome, "audit_outcome"), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_events_created_at",
        "audit_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_actor_created",
        "audit_events",
        ["actor_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_action_created",
        "audit_events",
        ["action", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_trace_id",
        "audit_events",
        ["trace_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["resource_type", "resource_id"],
        unique=False,
    )


def downgrade() -> None:
    """回滚审计事件表。"""
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_index("ix_audit_events_trace_id", table_name="audit_events")
    op.drop_index("ix_audit_events_action_created", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_created", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")