"""C1 模型与枚举映射的离线单测。"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum as PyEnum
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, create_engine
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.engine import Engine
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from app.db.models import (
    AuditReport,
    AuditRisk,
    AuditTask,
    AuthSession,
    Base,
    Contract,
    ContractCommit,
    ContractElement,
    SealDetection,
    SettingsRule,
    User,
    UserFeedback,
)
from app.generated.schemas import (
    ConflictType,
    ContractStatus,
    FeedbackVerdict,
    RiskCategory,
    RiskLevel,
    Status,
    TargetType,
)
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType

EXPECTED_TABLES = {
    "audit_reports",
    "audit_risks",
    "audit_tasks",
    "auth_sessions",
    "contract_elements",
    "contract_commits",
    "contracts",
    "seal_detections",
    "settings_rules",
    "user_feedbacks",
    "users",
}

MODELS = (
    Contract,
    ContractCommit,
    ContractElement,
    AuditTask,
    AuditRisk,
    AuthSession,
    SealDetection,
    SettingsRule,
    AuditReport,
    User,
    UserFeedback,
)


def _enum_class(model: type[Base], attr: str) -> type[PyEnum]:
    column_type = model.__table__.c[attr].type
    assert isinstance(column_type, SqlEnum)
    assert column_type.enum_class is not None
    return column_type.enum_class


def _foreign_keys(model: type[Base]) -> set[str]:
    return {key.target_fullname for key in model.__table__.foreign_keys}


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """建立七表内存 SQLite，验证模型本身可离线构造。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_models_and_table_names() -> None:
    assert {model.__tablename__ for model in MODELS} == EXPECTED_TABLES
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_contract_element_projects_all_contract_chunk_fields() -> None:
    chunk_fields = set(ContractChunk.model_fields)
    element_fields = set(ContractElement.__table__.c.keys())

    assert chunk_fields | {"contract_id", "vector_id"} == element_fields


def test_enum_columns_use_contract_enum_types() -> None:
    assert _enum_class(Contract, "status") is ContractStatus
    assert _enum_class(Contract, "contract_type") is ContractType
    assert _enum_class(ContractElement, "source_type") is SourceType
    assert _enum_class(ContractElement, "chunk_type") is ChunkType
    assert _enum_class(ContractElement, "contract_type") is ContractType
    assert _enum_class(AuditTask, "status") is Status
    assert _enum_class(AuditRisk, "category") is RiskCategory
    assert _enum_class(AuditRisk, "level") is RiskLevel
    assert _enum_class(AuditRisk, "conflict_type") is ConflictType
    assert _enum_class(AuditReport, "status") is Status
    assert _enum_class(UserFeedback, "target_type") is TargetType
    assert _enum_class(UserFeedback, "verdict") is FeedbackVerdict


def test_contract_rejects_invalid_contract_status(sqlite_engine) -> None:
    with Session(sqlite_engine) as session:
        session.add(
            Contract(
                id=uuid4(),
                filename="采购合同.docx",
                file_size=1024,
                file_hash="a" * 64,
                storage_path="contracts/purchase.docx",
                status="FAILED",
                contract_type=ContractType.PURCHASE,
            )
        )
        with pytest.raises(StatementError, match="FAILED"):
            session.flush()


def test_audit_risk_rejects_invalid_risk_level(sqlite_engine) -> None:
    with Session(sqlite_engine) as session:
        contract_id = uuid4()
        session.add(
            Contract(
                id=contract_id,
                filename="采购合同.docx",
                file_size=1024,
                file_hash="b" * 64,
                storage_path="contracts/purchase.docx",
                status=ContractStatus.parsing,
                contract_type=ContractType.PURCHASE,
            )
        )
        session.flush()
        session.add(
            AuditRisk(
                id="risk-1",
                contract_id=contract_id,
                level="INFO",
                suggestion="调整条款",
            )
        )
        with pytest.raises(StatementError, match="INFO"):
            session.flush()


def test_audit_task_progress_and_status_constraints() -> None:
    constraints = " ".join(
        str(constraint.sqltext)
        for constraint in AuditTask.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )

    assert "progress >= 0.0 AND progress <= 1.0" in constraints
    assert _enum_class(AuditTask, "status") is Status


def test_user_feedback_uses_polymorphic_target_columns() -> None:
    columns = set(UserFeedback.__table__.c.keys())

    assert {"target_type", "target_id"} <= columns
    assert "risk_id" not in columns


def test_contract_foreign_keys_cover_persisted_child_tables() -> None:
    assert "contracts.id" in _foreign_keys(ContractCommit)
    assert "contracts.id" in _foreign_keys(ContractElement)
    assert "contracts.id" in _foreign_keys(AuditTask)
    assert "contracts.id" in _foreign_keys(AuditRisk)
    assert "contracts.id" in _foreign_keys(SealDetection)
    assert "contracts.id" in _foreign_keys(AuditReport)
