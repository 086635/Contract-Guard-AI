"""add task reliability fields

Revision ID: 20261001_0012
Revises: 20261001_0011
Create Date: 2026-10-01

对应：`文件集合/codex/4.C62任务可靠性模型与Celery策略.md`。

⚠️ 本迁移**重建** `audit_tasks`（rename → create → 迁移数据 → drop）而不是 `ALTER TABLE`：
   1. 需要新增 7 个可靠性列（ALTER 也能做）；
   2. 更需要**放宽 `audit_task_type` CHECK**（新增 SCAN_UPLOAD / CONVERT_DOC / REWRITE）——
      该约束是 `CREATE TABLE` 内联的，SQLite/离线迁移守卫不支持 `DROP CONSTRAINT`；
      重建语句（RENAME/CREATE/INSERT SELECT/DROP）在 PostgreSQL 与 SQLite 上都是合法 SQL。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261001_0012"
down_revision: str | Sequence[str] | None = "20261001_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 迁移前的类型白名单（0001 建的约束）
TASK_TYPES_BEFORE = ("PARSE_DOC", "RUN_AUDIT", "GEN_REPORT")
#: 迁移后的类型白名单（与 `app.db.models._AUDIT_TASK_TYPES` 一致）
TASK_TYPES_AFTER = (
    "PARSE_DOC",
    "RUN_AUDIT",
    "GEN_REPORT",
    "REWRITE",
    "SCAN_UPLOAD",
    "CONVERT_DOC",
)

BASE_COLUMNS = (
    "id",
    "contract_id",
    "task_type",
    "stage",
    "progress",
    "status",
    "celery_task_id",
    "error_message",
    "created_at",
    "updated_at",
)
RELIABILITY_COLUMNS = (
    "attempt_count",
    "max_attempts",
    "last_heartbeat_at",
    "next_retry_at",
    "error_code",
    "locked_at",
    "completed_at",
)


def _task_type_enum(values: Sequence[str]) -> sa.Enum:
    """与 ORM `_enum_type(..., "audit_task_type")` 同形的非原生枚举。"""
    return sa.Enum(
        *values,
        name="audit_task_type",
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


def _table_columns(*, with_reliability: bool) -> list[sa.Column]:
    """`audit_tasks` 的列定义（与 `app.db.models.AuditTask` 逐列一致）。"""
    columns = [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column(
            "task_type",
            _task_type_enum(
                TASK_TYPES_AFTER if with_reliability else TASK_TYPES_BEFORE
            ),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "RUNNING",
                "DONE",
                "FAILED",
                name="task_status",
                native_enum=False,
                create_constraint=True,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    ]
    if with_reliability:
        columns.extend(
            [
                sa.Column(
                    "attempt_count",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("0"),
                ),
                sa.Column(
                    "max_attempts",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("3"),
                ),
                sa.Column(
                    "last_heartbeat_at", sa.DateTime(timezone=True), nullable=True
                ),
                sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("error_code", sa.String(length=64), nullable=True),
                sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            ]
        )
    columns.extend(
        [
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
        ]
    )
    return columns


def _rebuild_audit_tasks(*, with_reliability: bool) -> None:
    """便携式重建：drop 索引 → rename → create → 迁移数据 → drop 旧表。"""
    op.drop_index("ix_audit_tasks_contract_type", table_name="audit_tasks")
    op.drop_index("ix_audit_tasks_status", table_name="audit_tasks")
    op.rename_table("audit_tasks", "audit_tasks_old")
    op.create_table(
        "audit_tasks",
        *_table_columns(with_reliability=with_reliability),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "progress >= 0.0 AND progress <= 1.0",
            name="ck_audit_tasks_progress",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
    )
    op.create_index(
        "ix_audit_tasks_contract_type",
        "audit_tasks",
        ["contract_id", "task_type"],
        unique=False,
    )
    op.create_index("ix_audit_tasks_status", "audit_tasks", ["status"], unique=False)
    # 只迁移**基础列**：新表的新增列走 server_default 回填（旧表没有这些列）。
    column_list = ", ".join(BASE_COLUMNS)
    op.execute(
        f"INSERT INTO audit_tasks ({column_list}) "
        f"SELECT {column_list} FROM audit_tasks_old"
    )
    op.drop_table("audit_tasks_old")


def upgrade() -> None:
    """新增可靠性字段，并放宽任务类型白名单（含 C57 扫描 / C59 转换）。"""
    _rebuild_audit_tasks(with_reliability=True)


def downgrade() -> None:
    """回退可靠性字段与类型白名单（新增列全部丢弃）。"""
    _rebuild_audit_tasks(with_reliability=False)
