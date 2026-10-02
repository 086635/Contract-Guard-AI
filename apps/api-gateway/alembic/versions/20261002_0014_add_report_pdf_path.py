"""add report pdf storage path

Revision ID: 20261002_0014
Revises: 20261002_0013
Create Date: 2026-10-02

对应：`文件集合/codex/4.C72报告独立Word与PDF导出.md`（双格式报告产物）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_0014"
down_revision: str | Sequence[str] | None = "20261002_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """给 audit_reports 增加 PDF 派生对象 key（NULL = 无 PDF）。"""
    op.add_column(
        "audit_reports",
        sa.Column("pdf_storage_path", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    """回滚 PDF 派生对象 key。"""
    op.drop_column("audit_reports", "pdf_storage_path")
