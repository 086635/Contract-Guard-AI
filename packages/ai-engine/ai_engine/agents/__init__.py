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
from .conflict import ConflictType, CrossClauseConflict, analyze_conflicts
from .errors import RewriteUnavailableError
from .rewriter import (
    AgentRewriteRequest,
    RewriteResponse,
    ScopedClauseMemory,
    rewrite_clause,
)
from .tools import RagTool, build_rag_tool

__all__ = [
    "AgentRewriteRequest",
    "AuditContext",
    "ConflictType",
    "CrossClauseConflict",
    "EnhancedRiskItem",
    "PreciseCitation",
    "RagTool",
    "ReasoningStep",
    "RewriteResponse",
    "RewriteUnavailableError",
    "RiskCategory",
    "RiskLevel",
    "ScopedClauseMemory",
    "analyze_conflicts",
    "audit_clause",
    "build_rag_tool",
    "rewrite_clause",
]
