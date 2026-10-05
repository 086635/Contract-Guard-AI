"""Persist complete rule editor state and optimistic version.

Revision ID: 20261005_0016
Revises: 20261003_0015
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261005_0016"
down_revision = "20261003_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("settings_rules", sa.Column("editor_state", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("settings_rules", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("settings_rules", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE settings_rules SET updated_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.drop_column("settings_rules", "updated_at")
    op.drop_column("settings_rules", "version")
    op.drop_column("settings_rules", "editor_state")
