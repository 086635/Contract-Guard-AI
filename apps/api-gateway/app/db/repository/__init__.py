"""数据库仓储层公共出口。"""

from __future__ import annotations

from app.db.repository.config_feedback import (
    FeedbackRecord,
    FeedbackRepository,
    RuleRecord,
    RuleRepository,
    RuleUpsert,
)
from app.db.repository.contract import (
    CommitItemRecord,
    CommitRepository,
    ContractCreate,
    ContractDetail,
    ContractRepository,
    ContractSummary,
    TaskRecord,
    TaskRepository,
)
from app.db.repository.result import (
    ElementRepository,
    ReportRecord,
    ReportRepository,
    RiskRepository,
    SealRepository,
)

__all__ = [
    "CommitItemRecord",
    "CommitRepository",
    "ContractCreate",
    "ContractDetail",
    "ContractRepository",
    "ContractSummary",
    "ElementRepository",
    "FeedbackRecord",
    "FeedbackRepository",
    "ReportRecord",
    "ReportRepository",
    "RiskRepository",
    "RuleRecord",
    "RuleRepository",
    "RuleUpsert",
    "SealRepository",
    "TaskRecord",
    "TaskRepository",
]
