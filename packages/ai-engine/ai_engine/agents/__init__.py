"""智能体定义包：auditor / rewriter / conflict。"""

from .auditor import (
    AuditContext,
    EnhancedRiskItem,
    PreciseCitation,
    ReasoningStep,
    RiskCategory,
    RiskLevel,
    audit_clause,
)
from .conflict import CrossClauseConflict, ConflictType, analyze_conflicts
from .rewriter import (
    AgentRewriteRequest,
    RewriteResponse,
    ScopedClauseMemory,
    rewrite_clause,
)

__all__ = [
    "AuditContext",
    "EnhancedRiskItem",
    "PreciseCitation",
    "ReasoningStep",
    "RiskCategory",
    "RiskLevel",
    "audit_clause",
    "CrossClauseConflict",
    "ConflictType",
    "analyze_conflicts",
    "AgentRewriteRequest",
    "RewriteResponse",
    "ScopedClauseMemory",
    "rewrite_clause",
]
