"""交付模块 C1：PostgreSQL 表模型与契约枚举映射。

设计要点：
1. ORM 表是 PostgreSQL 持久化的唯一定义处，字段来源于冻结契约。
2. 枚举直接复用 OpenAPI 生成物与 `rag_contracts`，避免第二套状态/类型定义。
3. `ContractElement` 完整投影 `ContractChunk` 28 字段，并补充合同外键与向量 ID。
4. 通用 SQLAlchemy 类型保持离线模型测试可运行，同时兼容 PostgreSQL。

对应：`文件集合/codex/4.C1表模型与枚举映射.md`。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum as PyEnum
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.generated.schemas import (
    ConflictType,
    ContractStatus,
    FeedbackVerdict,
    PreviewStatus,
    RiskCategory,
    RiskLevel,
    Role,
    SealStatus,
    SealType,
    Status,
    TargetType,
)
from rag_contracts.chunk import EMBEDDING_DIM, SCHEMA_VERSION
from rag_contracts.enums import ChunkType, ContractType, SourceType


def _default_review_thresholds() -> dict[str, float]:
    """返回设置页默认人工复核阈值。"""
    return {"high": 0.85, "medium": 0.65, "low": 0.40}


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)


def _enum_type(enum_cls: type[PyEnum], name: str) -> SqlEnum:
    """构造契约枚举列类型。

    Args:
        enum_cls: OpenAPI 或 `rag_contracts` 中的枚举类。
        name: 数据库约束名。

    Returns:
        使用枚举值落库、并拒绝非法字符串的 SQLAlchemy Enum。
    """
    return SqlEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


#: 网关允许落库的任务类型白名单（**唯一处**）：新增任务必须先登记到这里，
#: 否则 `audit_task_type` CHECK 约束会在写库时拒绝（C57 扫描 / C59 转换即为此补齐）。
_AUDIT_TASK_TYPES = (
    "PARSE_DOC",
    "RUN_AUDIT",
    "GEN_REPORT",
    "REWRITE",
    "SCAN_UPLOAD",
    "CONVERT_DOC",
)

#: 任务默认可尝试次数（C62 有限重试上限；单个任务可用 `max_attempts` 覆盖）。
DEFAULT_MAX_ATTEMPTS = 3


class AuditAction(StrEnum):
    """审计动作白名单；新增动作必须先登记到这里。"""

    login = "LOGIN"
    refresh = "REFRESH"
    logout = "LOGOUT"
    upload = "UPLOAD"
    view = "VIEW"
    rewrite = "REWRITE"
    commit = "COMMIT"
    report = "REPORT"
    export = "EXPORT"
    settings_update = "SETTINGS_UPDATE"
    user_create = "USER_CREATE"
    user_update = "USER_UPDATE"
    auth_denied = "AUTH_DENIED"
    task_recovered = "TASK_RECOVERED"
    file_rejected = "FILE_REJECTED"


class AuditOutcome(StrEnum):
    """审计结果枚举。"""

    success = "SUCCESS"
    failure = "FAILURE"
    denied = "DENIED"


class Base(DeclarativeBase):
    """ORM 声明式基类。"""


class Contract(Base):
    """合同主表。"""

    __tablename__ = "contracts"
    __table_args__ = (
        Index("ix_contracts_status", "status"),
        Index("ix_contracts_contract_type", "contract_type"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    filename: Mapped[str] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(BigInteger)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True)
    storage_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[ContractStatus] = mapped_column(
        _enum_type(ContractStatus, "contract_status"),
        default=ContractStatus.uploading,
    )
    contract_type: Mapped[ContractType] = mapped_column(
        _enum_type(ContractType, "contract_type"),
        default=ContractType.OTHER,
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class ContractElement(Base):
    """合同切片表：`ContractChunk` 28 字段投影 + 合同外键 + 向量 ID。"""

    __tablename__ = "contract_elements"
    __table_args__ = (
        CheckConstraint("level >= 0 AND level <= 8", name="ck_contract_elements_level"),
        CheckConstraint("seq_in_doc >= 0", name="ck_contract_elements_seq_in_doc"),
        CheckConstraint("char_count >= 0", name="ck_contract_elements_char_count"),
        CheckConstraint("token_count >= 0", name="ck_contract_elements_token_count"),
        CheckConstraint(
            "page_no IS NULL OR page_no >= 1",
            name="ck_contract_elements_page_no",
        ),
        CheckConstraint(
            f"embedding_dim IS NULL OR embedding_dim = {EMBEDDING_DIM}",
            name="ck_contract_elements_embedding_dim",
        ),
        CheckConstraint(
            "source_type != 'LAW_ARTICLE' OR law_code IS NOT NULL",
            name="ck_contract_elements_law_code",
        ),
        CheckConstraint(
            "chunk_type != 'WINDOW_SLICE' OR parent_section_id IS NOT NULL",
            name="ck_contract_elements_parent_section_id",
        ),
        CheckConstraint(
            "vector_id = chunk_id",
            name="ck_contract_elements_vector_id",
        ),
        Index(
            "ix_contract_elements_contract_seq",
            "contract_id",
            "seq_in_doc",
        ),
        Index("ix_contract_elements_section_id", "section_id"),
        Index("ix_contract_elements_source_type", "source_type"),
    )

    schema_version: Mapped[str] = mapped_column(String(16), default=SCHEMA_VERSION)
    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(200), index=True)
    source_type: Mapped[SourceType] = mapped_column(
        _enum_type(SourceType, "source_type")
    )
    chunk_type: Mapped[ChunkType] = mapped_column(_enum_type(ChunkType, "chunk_type"))
    section_id: Mapped[str] = mapped_column(String(64))
    law_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    law_title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    article_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_section_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    section_path: Mapped[str] = mapped_column(String(512), default="")
    level: Mapped[int] = mapped_column(SmallInteger, default=0)
    seq_in_doc: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(16))
    char_count: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    contract_type: Mapped[ContractType | None] = mapped_column(
        _enum_type(ContractType, "element_contract_type"),
        nullable=True,
    )
    category: Mapped[str] = mapped_column(String(64), default="UNSPECIFIED")
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(
        Integer, default=EMBEDDING_DIM, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    vector_id: Mapped[str] = mapped_column(String(64), unique=True)


class ContractCommit(Base):
    """合同提交批次明细。"""

    __tablename__ = "contract_commits"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            "item_id",
            name="uq_contract_commits_request_item",
        ),
        Index("ix_contract_commits_contract_id", "contract_id"),
        Index("ix_contract_commits_request_id", "request_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    request_id: Mapped[str] = mapped_column(String(64))
    item_id: Mapped[str] = mapped_column(String(64))
    accepted: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class AuditTask(Base):
    """审核任务表。"""

    __tablename__ = "audit_tasks"
    __table_args__ = (
        CheckConstraint(
            "progress >= 0.0 AND progress <= 1.0",
            name="ck_audit_tasks_progress",
        ),
        Index("ix_audit_tasks_contract_type", "contract_id", "task_type"),
        Index("ix_audit_tasks_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(
        SqlEnum(
            *_AUDIT_TASK_TYPES,
            name="audit_task_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
        )
    )
    stage: Mapped[str] = mapped_column(String(64))
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[Status] = mapped_column(
        _enum_type(Status, "task_status"),
        default=Status.running,
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ---- 可靠性字段（C62）：DB 任务行是唯一可恢复状态源，Redis 只做进度/队列 ----
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=DEFAULT_MAX_ATTEMPTS)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class AuditRisk(Base):
    """审核结果表：风险与跨条款矛盾的投影。"""

    __tablename__ = "audit_risks"
    __table_args__ = (Index("ix_audit_risks_contract_level", "contract_id", "level"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    clause_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[RiskCategory | None] = mapped_column(
        _enum_type(RiskCategory, "risk_category"),
        nullable=True,
    )
    level: Mapped[RiskLevel] = mapped_column(_enum_type(RiskLevel, "risk_level"))
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    bbox: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    suggestion: Mapped[str] = mapped_column(Text)
    legal_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_clause_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_clause_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conflict_type: Mapped[ConflictType | None] = mapped_column(
        _enum_type(ConflictType, "conflict_type"),
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class SealDetection(Base):
    """印章与签名检测结果表。"""

    __tablename__ = "seal_detections"
    __table_args__ = (
        CheckConstraint("page >= 1", name="ck_seal_detections_page"),
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_seal_detections_confidence",
        ),
        Index("ix_seal_detections_contract_id", "contract_id"),
        Index("ix_seal_detections_type_status", "type", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    type: Mapped[SealType] = mapped_column(_enum_type(SealType, "seal_type"))
    status: Mapped[SealStatus] = mapped_column(_enum_type(SealStatus, "seal_status"))
    bbox: Mapped[dict[str, Any]] = mapped_column(JSON)
    page: Mapped[int] = mapped_column(Integer)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_matched_with_parties: Mapped[bool] = mapped_column(Boolean)
    compliance_note: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)


class SettingsRule(Base):
    """审计规则配置表。"""

    __tablename__ = "settings_rules"
    __table_args__ = (Index("ix_settings_rules_category", "category"),)

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    rule_name: Mapped[str] = mapped_column(String(255), unique=True)
    category: Mapped[str] = mapped_column(String(64))
    prompt_template: Mapped[str] = mapped_column(Text)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    review_thresholds: Mapped[dict[str, float]] = mapped_column(
        JSON, default=_default_review_thresholds, nullable=False
    )


class User(Base):
    """本地账号表（密码哈希由后续 AuthService 生成）。"""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        Index("ix_users_role", "role"),
        Index("ix_users_is_active", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(_enum_type(Role, "user_role"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    display_name: Mapped[str] = mapped_column(String(255))
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class AuthSession(Base):
    """refresh 会话表；只持久化 refresh token 哈希，不保存明文。"""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint(
            "refresh_token_hash",
            name="uq_auth_sessions_refresh_token_hash",
        ),
        Index("ix_auth_sessions_user_id", "user_id"),
        Index("ix_auth_sessions_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("auth_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditEvent(Base):
    """append-only 审计事件表；应用仓储不提供 update/delete。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_created_at", "created_at"),
        Index("ix_audit_events_actor_created", "actor_id", "created_at"),
        Index("ix_audit_events_action_created", "action", "created_at"),
        Index("ix_audit_events_trace_id", "trace_id"),
        Index("ix_audit_events_resource", "resource_type", "resource_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    actor_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    actor_role: Mapped[Role | None] = mapped_column(
        _enum_type(Role, "audit_actor_role"), nullable=True
    )
    action: Mapped[AuditAction] = mapped_column(_enum_type(AuditAction, "audit_action"))
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[AuditOutcome] = mapped_column(
        _enum_type(AuditOutcome, "audit_outcome")
    )
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class AuditReport(Base):
    """审核报告元数据表。"""

    __tablename__ = "audit_reports"
    __table_args__ = (
        CheckConstraint(
            "format = 'DOCX'",
            name="ck_audit_reports_format",
        ),
        CheckConstraint(
            "download_count >= 0",
            name="ck_audit_reports_download_count",
        ),
        Index("ix_audit_reports_contract_id", "contract_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    format: Mapped[str] = mapped_column(String(16), default="DOCX")
    storage_path: Mapped[str] = mapped_column(String(1024))
    #: C72：PDF 派生对象 key；NULL = 该报告只有 DOCX（PDF 导出失败/历史数据）
    pdf_storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    status: Mapped[Status] = mapped_column(
        _enum_type(Status, "report_status"),
        default=Status.done,
    )


class UserFeedback(Base):
    """用户反馈表：使用目标类型与目标 ID 做多态关联。"""

    __tablename__ = "user_feedbacks"
    __table_args__ = (
        Index(
            "ix_user_feedbacks_target",
            "target_type",
            "target_id",
        ),
        Index(
            "ix_user_feedbacks_contract_created",
            "contract_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    target_type: Mapped[TargetType] = mapped_column(
        _enum_type(TargetType, "feedback_target_type")
    )
    target_id: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[FeedbackVerdict] = mapped_column(
        _enum_type(FeedbackVerdict, "feedback_verdict")
    )
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )


class ContractPreview(Base):
    """合同预览元数据（C58）：显式状态机 + 预览派生对象 key。

    `preview_object_key` 是**内部** MinIO key，只用于运行时签名，绝不进 API 响应；
    PDF 指向原对象，DOCX 由 `convert_doc`（C59）写入 `contract_previews/...` 派生对象。
    """

    __tablename__ = "contract_previews"
    __table_args__ = (
        UniqueConstraint("contract_id", name="uq_contract_previews_contract"),
        Index("ix_contract_previews_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    contract_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contracts.id"), nullable=False
    )
    status: Mapped[PreviewStatus] = mapped_column(
        _enum_type(PreviewStatus, "preview_status"),
        default=PreviewStatus.pending,
    )
    preview_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    convert_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    converter_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


class StandardClause(Base):
    """C70 标准条款库（人工维护源；检索索引由 C70 服务同步到 RAG collection）。"""

    __tablename__ = "standard_clauses"
    __table_args__ = (
        Index("ix_standard_clauses_type_enabled", "contract_type", "is_enabled"),
        Index("ix_standard_clauses_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    contract_type: Mapped[ContractType] = mapped_column(
        _enum_type(ContractType, "standard_clause_contract_type")
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(200), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )


__all__ = [
    "AuditAction",
    "AuditEvent",
    "AuditOutcome",
    "AuditReport",
    "AuditRisk",
    "AuditTask",
    "AuthSession",
    "Base",
    "Contract",
    "ContractCommit",
    "ContractElement",
    "ContractPreview",
    "SealDetection",
    "SettingsRule",
    "StandardClause",
    "User",
    "UserFeedback",
]
