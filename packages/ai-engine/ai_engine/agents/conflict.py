"""跨条款矛盾分析 Agent（conflict）。

职责：识别同一合同内相互矛盾的条款（付款 / 责任 / 期限），产出
CrossClauseConflict（§8.2）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from .auditor import PreciseCitation, RiskLevel


class ConflictType(str, Enum):
    PAYMENT_CONTRADICTION = "PAYMENT_CONTRADICTION"
    LIABILITY_CONTRADICTION = "LIABILITY_CONTRADICTION"
    TERM_CONTRADICTION = "TERM_CONTRADICTION"
    OTHER = "OTHER"


class CrossClauseConflict(BaseModel):
    """§8.2 跨条款矛盾"""

    id: str
    sourceClauseId: str
    targetClauseId: str
    conflictType: ConflictType
    description: str
    level: RiskLevel
    suggestion: str
    citations: list[PreciseCitation] = Field(default_factory=list)


def analyze_conflicts(
    contract_id: str,
    clause_pairs: list[tuple[str, str, str]],
) -> list[CrossClauseConflict]:
    """对给定条款对执行矛盾分析。

    :param clause_pairs: [(clause_id_a, clause_id_b, clause_text)] 列表
    TODO（工程实现）：调用 conflict Agent 比对条款语义，输出 CrossClauseConflict。
    """
    raise NotImplementedError("conflict.analyze_conflicts 待实现：接入 LLM 比对")
