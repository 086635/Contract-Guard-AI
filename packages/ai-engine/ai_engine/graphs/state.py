"""Agent 图状态定义。

设计要点：
1. 状态只承载可序列化数据，依赖对象由 C4 的 `GraphDeps` 闭包注入。
2. 追加型字段统一使用 `operator.add`，覆盖型字段保持后写覆盖语义。
3. 六阶段名只在本模块定义，SSE / 阶段日志 / 埋点均从这里派生。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ai_engine.agents.auditor import EnhancedRiskItem
from ai_engine.agents.conflict import CrossClauseConflict
from cg_observability import get_logger, log_event
from rag_contracts.enums import ContractType

AUDIT_STAGES: tuple[str, ...] = (
    "classify",
    "extract",
    "risk",
    "conflict",
    "standard_compare",
    "report_draft",
)

_LOGGER = get_logger("ai_engine.graphs.state")


class ClauseInput(TypedDict):
    """审核图的最小条款输入面。"""

    clause_id: str
    label: str
    text: str
    level: int
    parent_id: str | None
    page_no: int | None
    bbox: dict | None


class StageLog(TypedDict):
    """单阶段执行记录。"""

    stage: str
    status: str
    latency_ms: int
    detail: str


class AuditState(TypedDict, total=False):
    """审核图状态。"""

    # 入口注入（只读）
    contract_id: str
    trace_id: str
    tolerance: str
    clauses: list[ClauseInput]

    # 各节点产出
    contract_type: ContractType | None
    elements: list[dict]
    risks: Annotated[list[EnhancedRiskItem], operator.add]
    conflicts: Annotated[list[CrossClauseConflict], operator.add]
    missing_clauses: Annotated[list[dict], operator.add]
    report_draft: dict
    health_score: int

    # 观测与降级
    warnings: Annotated[list[str], operator.add]
    stage_log: Annotated[list[StageLog], operator.add]
    llm_calls: Annotated[int, operator.add]
    rag_calls: Annotated[int, operator.add]


class RewriteState(TypedDict, total=False):
    """改写图状态。"""

    contract_id: str
    clause_id: str
    trace_id: str
    instruction: str
    before: str
    after: str
    diff: dict
    reasoning_steps: list[dict]
    citations: list[dict]
    rag_context: dict | None
    warnings: Annotated[list[str], operator.add]
    stage_log: Annotated[list[StageLog], operator.add]


def new_audit_state(
    contract_id: str,
    clauses: list[ClauseInput],
    *,
    trace_id: str,
    tolerance: str = "BALANCED",
) -> AuditState:
    """构造审核图初始状态。"""
    state: AuditState = {
        "contract_id": contract_id,
        "trace_id": trace_id,
        "tolerance": tolerance,
        "clauses": clauses,
        "contract_type": None,
        "elements": [],
        "risks": [],
        "conflicts": [],
        "missing_clauses": [],
        "report_draft": {},
        "health_score": 0,
        "warnings": [],
        "stage_log": [],
        "llm_calls": 0,
        "rag_calls": 0,
    }
    log_event(
        _LOGGER,
        "graph.state.init",
        trace_id=trace_id,
        contract_id=contract_id,
        clause_count=len(clauses),
        tolerance=tolerance,
    )
    return state


__all__ = [
    "AUDIT_STAGES",
    "AuditState",
    "ClauseInput",
    "RewriteState",
    "StageLog",
    "new_audit_state",
]
