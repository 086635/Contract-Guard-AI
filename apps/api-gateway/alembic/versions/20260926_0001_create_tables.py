"""create delivery data tables

Revision ID: 20260926_0001
Revises:
Create Date: 2026-09-26

对应：`文件集合/codex/4.C2Alembic迁移基线.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum as PyEnum

import sqlalchemy as sa
from alembic import op

from app.generated.schemas import (
    ConflictType,
    ContractStatus,
    FeedbackVerdict,
    RiskCategory,
    RiskLevel,
    Status,
    TargetType,
)
from rag_contracts.chunk import EMBEDDING_DIM
from rag_contracts.enums import ChunkType, ContractType, SourceType

# revision identifiers, used by Alembic.
revision: str = "20260926_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_TASK_TYPES = ("PARSE_DOC", "RUN_AUDIT", "GEN_REPORT")


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
    """创建 7 张交付数据表。"""
    op.create_table(
        "contracts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column(
            "status",
            _enum(ContractStatus, "contract_status"),
            nullable=False,
        ),
        sa.Column(
            "contract_type",
            _enum(ContractType, "contract_type"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_hash"),
    )
    op.create_index("ix_contracts_status", "contracts", ["status"], unique=False)
    op.create_index(
        "ix_contracts_contract_type",
        "contracts",
        ["contract_type"],
        unique=False,
    )

    op.create_table(
        "settings_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("prompt_template", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_name"),
    )
    op.create_index(
        "ix_settings_rules_category",
        "settings_rules",
        ["category"],
        unique=False,
    )

    op.create_table(
        "contract_elements",
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=200), nullable=False),
        sa.Column(
            "source_type",
            _enum(SourceType, "source_type"),
            nullable=False,
        ),
        sa.Column(
            "chunk_type",
            _enum(ChunkType, "chunk_type"),
            nullable=False,
        ),
        sa.Column("section_id", sa.String(length=64), nullable=False),
        sa.Column("law_code", sa.String(length=64), nullable=True),
        sa.Column("law_title", sa.String(length=200), nullable=True),
        sa.Column("article_no", sa.String(length=64), nullable=True),
        sa.Column("parent_section_id", sa.String(length=64), nullable=True),
        sa.Column("section_path", sa.String(length=512), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("seq_in_doc", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=16), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=True),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column(
            "contract_type",
            _enum(ContractType, "element_contract_type"),
            nullable=True,
        ),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("vector_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "level >= 0 AND level <= 8",
            name="ck_contract_elements_level",
        ),
        sa.CheckConstraint(
            "seq_in_doc >= 0",
            name="ck_contract_elements_seq_in_doc",
        ),
        sa.CheckConstraint(
            "char_count >= 0",
            name="ck_contract_elements_char_count",
        ),
        sa.CheckConstraint(
            "token_count >= 0",
            name="ck_contract_elements_token_count",
        ),
        sa.CheckConstraint(
            "page_no IS NULL OR page_no >= 1",
            name="ck_contract_elements_page_no",
        ),
        sa.CheckConstraint(
            f"embedding_dim IS NULL OR embedding_dim = {EMBEDDING_DIM}",
            name="ck_contract_elements_embedding_dim",
        ),
        sa.CheckConstraint(
            "source_type != 'LAW_ARTICLE' OR law_code IS NOT NULL",
            name="ck_contract_elements_law_code",
        ),
        sa.CheckConstraint(
            "chunk_type != 'WINDOW_SLICE' OR parent_section_id IS NOT NULL",
            name="ck_contract_elements_parent_section_id",
        ),
        sa.CheckConstraint(
            "vector_id = chunk_id",
            name="ck_contract_elements_vector_id",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.UniqueConstraint("vector_id"),
    )
    op.create_index(
        "ix_contract_elements_contract_seq",
        "contract_elements",
        ["contract_id", "seq_in_doc"],
        unique=False,
    )
    op.create_index(
        "ix_contract_elements_doc_id",
        "contract_elements",
        ["doc_id"],
        unique=False,
    )
    op.create_index(
        "ix_contract_elements_section_id",
        "contract_elements",
        ["section_id"],
        unique=False,
    )
    op.create_index(
        "ix_contract_elements_source_type",
        "contract_elements",
        ["source_type"],
        unique=False,
    )

    op.create_table(
        "audit_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column(
            "task_type",
            sa.Enum(
                *_AUDIT_TASK_TYPES,
                name="audit_task_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column(
            "status",
            _enum(Status, "task_status"),
            nullable=False,
        ),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "progress >= 0.0 AND progress <= 1.0",
            name="ck_audit_tasks_progress",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_tasks_contract_type",
        "audit_tasks",
        ["contract_id", "task_type"],
        unique=False,
    )
    op.create_index(
        "ix_audit_tasks_status",
        "audit_tasks",
        ["status"],
        unique=False,
    )

    op.create_table(
        "audit_risks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("clause_id", sa.String(length=64), nullable=True),
        sa.Column(
            "category",
            _enum(RiskCategory, "risk_category"),
            nullable=True,
        ),
        sa.Column(
            "level",
            _enum(RiskLevel, "risk_level"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("bbox", sa.JSON(), nullable=True),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("legal_basis", sa.Text(), nullable=True),
        sa.Column("reasoning_steps", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("source_clause_id", sa.String(length=64), nullable=True),
        sa.Column("target_clause_id", sa.String(length=64), nullable=True),
        sa.Column(
            "conflict_type",
            _enum(ConflictType, "conflict_type"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_risks_contract_level",
        "audit_risks",
        ["contract_id", "level"],
        unique=False,
    )

    op.create_table(
        "audit_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contract_id", sa.Uuid(), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("download_count", sa.Integer(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            _enum(Status, "report_status"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "format = 'DOCX'",
            name="ck_audit_reports_format",
        ),
        sa.CheckConstraint(
            "download_count >= 0",
            name="ck_audit_reports_download_count",
        ),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_reports_contract_id",
        "audit_reports",
        ["contract_id"],
        unique=False,
    )

    op.create_table(
        "user_feedbacks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "target_type",
            _enum(TargetType, "feedback_target_type"),
            nullable=False,
        ),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column(
            "verdict",
            _enum(FeedbackVerdict, "feedback_verdict"),
            nullable=False,
        ),
        sa.Column("user_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_feedbacks_target",
        "user_feedbacks",
        ["target_type", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    """回滚 7 张交付数据表。"""
    op.drop_index("ix_user_feedbacks_target", table_name="user_feedbacks")
    op.drop_table("user_feedbacks")

    op.drop_index("ix_audit_reports_contract_id", table_name="audit_reports")
    op.drop_table("audit_reports")

    op.drop_index("ix_audit_risks_contract_level", table_name="audit_risks")
    op.drop_table("audit_risks")

    op.drop_index("ix_audit_tasks_status", table_name="audit_tasks")
    op.drop_index("ix_audit_tasks_contract_type", table_name="audit_tasks")
    op.drop_table("audit_tasks")

    op.drop_index(
        "ix_contract_elements_source_type",
        table_name="contract_elements",
    )
    op.drop_index(
        "ix_contract_elements_section_id",
        table_name="contract_elements",
    )
    op.drop_index(
        "ix_contract_elements_doc_id",
        table_name="contract_elements",
    )
    op.drop_index(
        "ix_contract_elements_contract_seq",
        table_name="contract_elements",
    )
    op.drop_table("contract_elements")

    op.drop_index("ix_settings_rules_category", table_name="settings_rules")
    op.drop_table("settings_rules")

    op.drop_index("ix_contracts_contract_type", table_name="contracts")
    op.drop_index("ix_contracts_status", table_name="contracts")
    op.drop_table("contracts")
