"""风险审核 Agent（auditor）。

职责：对合同要素与条款进行大模型风险审核，产出 EnhancedRiskItem（§8.4 / §9）。
每个风险项须附带 CoT 推理链 ReasoningStep 与 RAG 精确溯源 PreciseCitation。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field


class RiskCategory(str, Enum):
    """风险类别（≥10 类，data_scheme.md §9）"""

    UNLIMITED_LIABILITY = "UNLIMITED_LIABILITY"
    PENALTY_TOO_HIGH = "PENALTY_TOO_HIGH"
    UNFAVORABLE_JURISDICTION = "UNFAVORABLE_JURISDICTION"
    UNILATERAL_TERMINATION = "UNILATERAL_TERMINATION"
    VAGUE_CONFIDENTIALITY = "VAGUE_CONFIDENTIALITY"
    MISSING_DISPUTE_CLAUSE = "MISSING_DISPUTE_CLAUSE"
    PAYMENT_TERM_RISK = "PAYMENT_TERM_RISK"
    LIABILITY_IMBALANCE = "LIABILITY_IMBALANCE"
    FORCE_MAJEURE_ABSENT = "FORCE_MAJEURE_ABSENT"
    IP_OWNERSHIP_RISK = "IP_OWNERSHIP_RISK"
    OTHER = "OTHER"


class RiskLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ReasoningStep(BaseModel):
    """§8.4 单步推理"""

    stepNumber: int = Field(ge=1)
    thought: str
    evidenceText: str


class PreciseCitation(BaseModel):
    """§8.4 RAG 精确溯源"""

    sourceType: str  # STANDARD_CLAUSE | LAW_ARTICLE | PRECEDENT
    sourceTitle: str
    contentChunk: str
    relevanceScore: float = Field(ge=0.0, le=1.0)


class EnhancedRiskItem(BaseModel):
    """§8.4 扩展风险项：RiskItem + CoT + 溯源"""

    id: str
    clauseId: str
    category: RiskCategory
    level: RiskLevel
    title: str
    originalText: str
    bbox: dict  # BBox (PDF_POINTS)
    suggestion: str
    legalBasis: str | None = None
    reasoningSteps: list[ReasoningStep] = Field(default_factory=list)
    citations: list[PreciseCitation] = Field(default_factory=list)


@dataclass
class AuditContext:
    """审核上下文（来自分层 Memory / RAG）"""

    contract_id: str
    clause_text: str
    rag_context: dict | None = None
    tolerance: str = "BALANCED"  # STRICT | BALANCED | LOOSE


def audit_clause(ctx: AuditContext) -> EnhancedRiskItem:
    """对单条款执行风险审核。

    TODO（工程实现）：接入 LLM + PromptRoleConfig（§8.3），结合 RAG 检索到的
    标准条款 / 法条，输出收敛到 EnhancedRiskItem 的结构化结果。
    """
    raise NotImplementedError("auditor.audit_clause 待实现：接入 LLM 与 RAG 上下文")
