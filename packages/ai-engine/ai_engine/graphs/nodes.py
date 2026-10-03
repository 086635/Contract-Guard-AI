"""审核图节点骨架与共享辅助函数。

设计要点：
1. C4 先提供可编译的占位节点，C5~C8 再依次替换各自业务实现。
2. 依赖 resolver、条款预算和阶段增量只在本模块定义一次。
3. 占位节点不调用模型、RAG 或外部 IO。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from ai_engine.agents.auditor import (
    EnhancedRiskItem,
    PreciseCitation,
    ReasoningStep,
    RiskCategory,
    RiskLevel,
)
from ai_engine.agents.conflict import ConflictType, CrossClauseConflict
from ai_engine.graphs.deps import GraphDeps
from ai_engine.graphs.state import AUDIT_STAGES, AuditState, ClauseInput
from ai_engine.llm.factory import get_chat_model
from ai_engine.llm.structured import invoke_structured
from ai_engine.prompts.loader import PromptTemplate
from cg_observability import get_logger, log_event
from rag_contracts.agent_tool import EMPTY_CONTEXT_HINT
from rag_contracts.element_keys import (
    ELEMENT_KEYS_PROMPT_BLOCK,
    is_allowed_key,
    is_core_key,
    label_for,
    normalize_key,
)
from rag_contracts.enums import ContractType, SourceType

_LOGGER = get_logger("ai_engine.graphs.nodes")


class _ClassifyOutput(BaseModel):
    """合同分类节点的私有结构化输出。"""

    contract_type: ContractType


class _ElementDraft(BaseModel):
    """LLM 要素补漏的私有输出；bbox 永远不由模型提供。"""

    key: str
    label: str = ""
    value: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    is_custom: bool = True


class _ExtractBatch(BaseModel):
    """要素补漏批输出。"""

    elements: list[_ElementDraft] = Field(default_factory=list)


class _RiskDraft(BaseModel):
    """风险节点的私有结构化输出。"""

    category: RiskCategory
    level: RiskLevel
    title: str
    suggestion: str
    reasoning_steps: list[ReasoningStep] = Field(default_factory=list)


class _ConflictDraft(BaseModel):
    """冲突精判的私有结构化输出。"""

    pair_index: int = Field(ge=0)
    conflict_type: ConflictType
    description: str
    level: RiskLevel
    suggestion: str


class _ConflictBatch(BaseModel):
    """冲突批输出。"""

    conflicts: list[_ConflictDraft] = Field(default_factory=list)


_CONFLICT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("付款", "支付", "价款", "结算", "发票"),
    ("违约", "赔偿", "责任", "免责", "不可抗力"),
    ("期限", "有效期", "提前", "终止", "续约"),
)


_RISK_DEDUCTION: dict[RiskLevel, int] = {
    RiskLevel.HIGH: 10,
    RiskLevel.MEDIUM: 5,
    RiskLevel.LOW: 2,
}
_CONFLICT_DEDUCTION: dict[RiskLevel, int] = {
    RiskLevel.HIGH: 8,
    RiskLevel.MEDIUM: 4,
    RiskLevel.LOW: 1,
}
_PERFORMANCE_RISK_CATEGORIES = {
    RiskCategory.UNLIMITED_LIABILITY,
    RiskCategory.LIABILITY_IMBALANCE,
    RiskCategory.PENALTY_TOO_HIGH,
    RiskCategory.PAYMENT_TERM_RISK,
    RiskCategory.UNILATERAL_TERMINATION,
}
_PERFORMANCE_CONFLICT_TYPES = {
    ConflictType.PAYMENT_CONTRADICTION,
    ConflictType.LIABILITY_CONTRADICTION,
}
_ERR_LLM_UNAVAILABLE = "ERR_LLM_UNAVAILABLE"
_ERR_LLM_ALL_CLAUSES_FAILED = "ERR_LLM_ALL_CLAUSES_FAILED"
_ERR_LLM_INVALID_RISK = "ERR_LLM_INVALID_RISK"
_ERR_LLM_INVALID_CONFLICT = "ERR_LLM_INVALID_CONFLICT"
_ERR_LLM_ALL_CONFLICTS_FAILED = "ERR_LLM_ALL_CONFLICTS_FAILED"


def _resolve_llm(deps: GraphDeps, role: str) -> BaseChatModel:
    """优先使用注入模型，否则按角色走唯一模型工厂。"""
    if deps.llm is not None:
        return deps.llm
    return get_chat_model(role)


def _resolve_prompt(deps: GraphDeps, name: str) -> PromptTemplate | None:
    """缺库或缺模板时返回 None，不向节点抛异常。"""
    if deps.prompt_lib is None:
        return None
    try:
        return deps.prompt_lib.get(name)
    except KeyError:
        return None


def _bounded_clauses(
    state: AuditState,
    deps: GraphDeps,
) -> tuple[list[ClauseInput], list[str]]:
    """按 max_clauses 截断条款，并返回固定预算 warning。"""
    clauses = list(state.get("clauses", []))
    limit = max(0, deps.max_clauses)
    if len(clauses) <= limit:
        return clauses, []
    warning = f"条款数 {len(clauses)} 超过上限 {limit}，仅审核前 {limit} 条"
    return clauses[:limit], [warning]


def _stage_update(
    stage: str,
    status: str,
    latency_ms: int,
    detail: str,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """构造 warnings + stage_log 状态增量。"""
    return {
        "warnings": list(warnings or []),
        "stage_log": [
            {
                "stage": stage,
                "status": status,
                "latency_ms": max(0, int(latency_ms)),
                "detail": detail,
            }
        ],
    }


def _placeholder(stage: str, component: str) -> dict[str, Any]:
    started = time.perf_counter()
    return _stage_update(
        stage,
        "SKIPPED",
        int((time.perf_counter() - started) * 1000),
        f"{component} 未实现：{stage} 节点",
    )


def _finish_stage(
    state: Mapping[str, Any],
    stage: str,
    status: str,
    started: float,
    detail: str,
    *,
    warnings: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写统一阶段日志，并合并节点业务增量。"""
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    trace_id = str(state.get("trace_id", "") or "-")
    log_event(
        _LOGGER,
        f"audit.{stage}",
        trace_id=trace_id,
        node_stage=stage,
        status=status,
        latency_ms=latency_ms,
        detail=detail,
    )
    result = _stage_update(
        stage,
        status,
        latency_ms,
        detail,
        warnings=warnings,
    )
    if extra:
        result.update(extra)
    return result


def _classify_excerpt(clauses: list[ClauseInput]) -> str:
    """构造分类模板所需的条款摘要。"""
    return "\n".join(
        f"{clause.get('label', '')}: {clause.get('text', '')}"
        for clause in clauses[:20]
    )


def _confidence(value: Any) -> float:
    """把任意置信度值夹取到 [0, 1]。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, parsed))


def _risk_draft_is_valid(draft: _RiskDraft | None) -> bool:
    """拒绝离线桩或残损结构化输出生成的空风险。"""
    return bool(draft is not None and draft.title.strip() and draft.suggestion.strip())


def _normalize_element(item: Any) -> dict[str, Any] | None:
    """把单个要素收敛到 OpenAPI ContractElement 的 6 个字段。"""
    if not isinstance(item, Mapping):
        return None
    # C80：别名先归一到核心 key（如 contract_amount → amount），再判词表归属。
    key = normalize_key(item.get("key", ""))
    if not key:
        return None

    bbox = item.get("bbox")
    model_dump = getattr(bbox, "model_dump", None)
    if callable(model_dump):
        bbox = model_dump()
    if not isinstance(bbox, dict):
        bbox = None

    value = item.get("value")
    return {
        "key": key,
        "label": str(item.get("label", "") or key),
        "value": None if value is None else str(value),
        "confidence": _confidence(item.get("confidence", 0.0)),
        "bbox": bbox,
        # C80 契约语义：核心词表 → isCustom=false；明细/其它 → true。
        "isCustom": not is_core_key(key),
    }


def _normalize_elements(
    elements: list[Any],
) -> tuple[list[dict[str, Any]], int, int]:
    """归一化并按 `(key, label, value)` 去重。"""
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    invalid = 0
    duplicate = 0

    for item in elements:
        normalized = _normalize_element(item)
        if normalized is None:
            invalid += 1
            continue
        dedup_key = (
            normalized["key"],
            normalized["label"],
            normalized["value"],
        )
        if dedup_key in seen:
            duplicate += 1
            continue
        seen.add(dedup_key)
        unique.append(normalized)

    return unique, duplicate, invalid


def _clause_block(clauses: list[ClauseInput]) -> str:
    """构造要素补漏模板所需的条款块。"""
    return "\n\n".join(
        f"[{clause.get('label', '')}] {clause.get('text', '')}" for clause in clauses
    )


def _draft_to_element(draft: _ElementDraft) -> dict[str, Any] | None:
    """把 LLM 补漏草稿转成纯 dict；**越界 key 返回 None**（由调用方丢弃并计数）。

    C80：key 必须先经 `normalize_key` 归一（别名映射到核心），再校验是否在
    「核心 ∪ 明细白名单」内；`isCustom` 按词表决定，不再硬编码 True。
    """
    key = normalize_key(draft.key)
    if not key or not is_allowed_key(key):
        return None
    return {
        "key": key,
        "label": draft.label.strip() or label_for(key, default=key),
        "value": draft.value,
        "confidence": _confidence(draft.confidence),
        "bbox": None,
        "isCustom": not is_core_key(key),
    }


def classify_node(state: AuditState, deps: GraphDeps) -> dict[str, Any]:
    """分类合同类型。"""
    stage = AUDIT_STAGES[0]
    started = time.perf_counter()
    clauses, budget_warnings = _bounded_clauses(state, deps)
    if not clauses:
        warning = "无条款，跳过合同分类"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"contract_type": None, "llm_calls": 0},
        )

    prompt = _resolve_prompt(deps, "classify")
    if prompt is None:
        warning = "缺少 classify 模板"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"contract_type": None, "llm_calls": 0},
        )

    attempted = False
    try:
        model = _resolve_llm(deps, "classify")
        rendered = prompt.render(
            contract_title=str(state.get("contract_id", "")),
            clause_excerpt=_classify_excerpt(clauses),
        )
        attempted = True
        value, warnings = invoke_structured(
            model,
            _ClassifyOutput,
            [HumanMessage(content=rendered)],
            trace_id=str(state.get("trace_id", "") or "-"),
        )
        if value is None:
            detail = "合同分类降级：模型未返回有效结果"
            return _finish_stage(
                state,
                stage,
                "DEGRADED",
                started,
                detail,
                warnings=[*budget_warnings, *warnings, detail],
                extra={"contract_type": None, "llm_calls": 1},
            )
        status = "DEGRADED" if warnings else "OK"
        return _finish_stage(
            state,
            stage,
            status,
            started,
            f"合同分类完成：{value.contract_type.value}",
            warnings=[*budget_warnings, *warnings],
            extra={"contract_type": value.contract_type, "llm_calls": 1},
        )
    except Exception as exc:  # noqa: BLE001 - 节点契约要求永不抛异常
        detail = f"合同分类失败：{type(exc).__name__}"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            detail,
            warnings=[*budget_warnings, detail],
            extra={"contract_type": None, "llm_calls": int(attempted)},
        )


def extract_node(state: AuditState, deps: GraphDeps) -> dict[str, Any]:
    """归一化外部 elements；仅在为空时允许 LLM 补漏。"""
    stage = AUDIT_STAGES[1]
    started = time.perf_counter()
    clauses, budget_warnings = _bounded_clauses(state, deps)
    raw_elements = list(state.get("elements", []))

    if raw_elements:
        elements, duplicates, invalid = _normalize_elements(raw_elements)
        warnings = list(budget_warnings)
        if invalid:
            warnings.append(f"忽略 {invalid} 个缺少 key 的要素")
        detail = f"归一化 {len(elements)} 个要素；去重 {duplicates} 个"
        status = "DEGRADED" if invalid else "OK"
        return _finish_stage(
            state,
            stage,
            status,
            started,
            detail,
            warnings=warnings,
            extra={"elements": elements, "llm_calls": 0},
        )

    if not clauses:
        warning = "无条款，跳过要素补漏"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"elements": [], "llm_calls": 0},
        )

    prompt = _resolve_prompt(deps, "extract")
    if prompt is None:
        warning = "缺少 extract 模板"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"elements": [], "llm_calls": 0},
        )

    attempted = False
    try:
        model = _resolve_llm(deps, "extract")
        rendered = prompt.render(
            clause_block=_clause_block(clauses),
            element_keys=ELEMENT_KEYS_PROMPT_BLOCK,
        )
        attempted = True
        value, warnings = invoke_structured(
            model,
            _ExtractBatch,
            [HumanMessage(content=rendered)],
            trace_id=str(state.get("trace_id", "") or "-"),
        )
        if value is None:
            detail = "要素补漏降级：模型未返回有效结果"
            return _finish_stage(
                state,
                stage,
                "DEGRADED",
                started,
                detail,
                warnings=[*budget_warnings, *warnings, detail],
                extra={"elements": [], "llm_calls": 1},
            )

        raw_drafts = [_draft_to_element(draft) for draft in value.elements]
        dropped = sum(1 for item in raw_drafts if item is None)
        drafts = [item for item in raw_drafts if item is not None]
        elements, duplicates, invalid = _normalize_elements(drafts)
        result_warnings = [*budget_warnings, *warnings]
        if invalid:
            result_warnings.append(f"忽略 {invalid} 个缺少 key 的补漏要素")
        if dropped:
            result_warnings.append(f"忽略 {dropped} 个 key 不在词表内的补漏要素")
        status = "DEGRADED" if warnings or invalid or dropped else "OK"
        return _finish_stage(
            state,
            stage,
            status,
            started,
            f"要素补漏完成：{len(elements)} 个要素，去重 {duplicates} 个",
            warnings=result_warnings,
            extra={"elements": elements, "llm_calls": 1},
        )
    except Exception as exc:  # noqa: BLE001 - 节点契约要求永不抛异常
        detail = f"要素补漏失败：{type(exc).__name__}"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            detail,
            warnings=[*budget_warnings, detail],
            extra={"elements": [], "llm_calls": int(attempted)},
        )


def risk_node(state: AuditState, deps: GraphDeps) -> dict[str, Any]:
    """逐条款执行 RAG 检索与风险识别。"""
    stage = AUDIT_STAGES[2]
    started = time.perf_counter()
    clauses, budget_warnings = _bounded_clauses(state, deps)
    if not clauses:
        warning = "无条款，跳过风险识别"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"risks": [], "rag_calls": 0, "llm_calls": 0},
        )

    prompt = _resolve_prompt(deps, "risk")
    if prompt is None:
        warning = "缺少 risk 模板"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"risks": [], "rag_calls": 0, "llm_calls": 0},
        )

    try:
        model = _resolve_llm(deps, "risk")
    except Exception as exc:  # noqa: BLE001 - 节点契约要求永不抛异常
        detail = f"{_ERR_LLM_UNAVAILABLE}: 风险模型不可用：{type(exc).__name__}"
        return _finish_stage(
            state,
            stage,
            "FAILED",
            started,
            detail,
            warnings=[*budget_warnings, detail],
            extra={"risks": [], "rag_calls": 0, "llm_calls": 0},
        )

    contract_type = state.get("contract_type")
    contract_type_value = (
        contract_type.value if isinstance(contract_type, ContractType) else ""
    )
    trace_id = str(state.get("trace_id", "") or "-")
    contract_id = str(state.get("contract_id", ""))
    risks: list[EnhancedRiskItem] = []
    warnings = list(budget_warnings)
    rag_calls = 0
    llm_calls = 0
    attempted_clauses = 0
    failed_clauses = 0
    degraded = False

    for ordinal, clause in enumerate(clauses, start=1):
        rag_result = None
        rag_context = EMPTY_CONTEXT_HINT
        citations: list[PreciseCitation] = []
        if deps.rag_tool is None:
            degraded = True
            warnings.append(f"条款 {clause['clause_id']} 无 RAG 工具，使用空召回提示")
        else:
            rag_calls += 1
            try:
                rag_result = deps.rag_tool(
                    query=clause["text"],
                    contract_type=contract_type,
                    source_types=[
                        SourceType.STANDARD_CLAUSE,
                        SourceType.LAW_ARTICLE,
                    ],
                    top_k=5,
                    reason="risk",
                    trace_id=trace_id,
                )
                rag_context = rag_result.context_block or EMPTY_CONTEXT_HINT
                citations = [
                    PreciseCitation.model_validate(citation.model_dump())
                    for citation in rag_result.citations
                ]
                if rag_result.status == "DEGRADED":
                    degraded = True
            except Exception as exc:  # noqa: BLE001 - RAG 单条失败必须隔离
                degraded = True
                rag_context = EMPTY_CONTEXT_HINT
                warnings.append(
                    f"条款 {clause['clause_id']} RAG 失败：{type(exc).__name__}"
                )

        try:
            attempted_clauses += 1
            rendered = prompt.render(
                clause_label=clause.get("label", ""),
                clause_text=clause.get("text", ""),
                contract_type=contract_type_value,
                tolerance=str(state.get("tolerance", "BALANCED")),
                rag_context=rag_context,
            )
            llm_calls += 1
            draft, invoke_warnings = invoke_structured(
                model,
                _RiskDraft,
                [HumanMessage(content=rendered)],
                trace_id=trace_id,
            )
            warnings.extend(invoke_warnings)
            if not _risk_draft_is_valid(draft):
                failed_clauses += 1
                degraded = True
                warning = (
                    f"{_ERR_LLM_INVALID_RISK}: 条款 {clause['clause_id']} 风险识别降级"
                    if draft is None
                    else f"{_ERR_LLM_INVALID_RISK}: 条款 {clause['clause_id']} 返回空标题或空建议"
                )
                warnings.append(warning)
                continue

            bbox = clause.get("bbox")
            if not isinstance(bbox, dict):
                bbox = {}
                warnings.append(f"条款 {clause['clause_id']} 缺少 bbox")
            if not draft.reasoning_steps:
                warnings.append(f"条款 {clause['clause_id']} reasoning_steps 为空")

            risks.append(
                EnhancedRiskItem(
                    id=f"{contract_id}:{clause['clause_id']}:risk:{ordinal}",
                    clauseId=clause["clause_id"],
                    category=draft.category,
                    level=draft.level,
                    title=draft.title,
                    originalText=clause["text"],
                    bbox=bbox,
                    suggestion=draft.suggestion,
                    legalBasis=None,
                    reasoningSteps=draft.reasoning_steps,
                    citations=citations,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单条款失败不得中断整图
            failed_clauses += 1
            degraded = True
            warnings.append(
                f"{_ERR_LLM_INVALID_RISK}: 条款 {clause['clause_id']} 风险识别失败：{type(exc).__name__}"
            )

    if attempted_clauses > 0 and failed_clauses == attempted_clauses:
        status = "FAILED"
        detail = f"风险识别失败：{failed_clauses} 个条款均未返回有效结果"
        warnings.append(f"{_ERR_LLM_ALL_CLAUSES_FAILED}: {detail}")
    else:
        status = "DEGRADED" if degraded else "OK"
        detail = f"风险识别完成：{len(risks)} 条风险"

    return _finish_stage(
        state,
        stage,
        status,
        started,
        detail,
        warnings=warnings,
        extra={
            "risks": risks,
            "rag_calls": rag_calls,
            "llm_calls": llm_calls,
        },
    )


def build_conflict_candidates(
    clauses: list[ClauseInput],
    max_pairs: int,
) -> list[tuple[ClauseInput, ClauseInput]]:
    """按关键词组粗筛、稳定排序并截断冲突候选对。"""
    if max_pairs <= 0:
        return []

    candidates: set[tuple[int, int]] = set()
    for group in _CONFLICT_GROUPS:
        indexes = [
            index
            for index, clause in enumerate(clauses)
            if any(keyword in clause.get("text", "") for keyword in group)
        ]
        for left_pos, left_index in enumerate(indexes):
            for right_index in indexes[left_pos + 1 :]:
                candidates.add((left_index, right_index))

    ordered = sorted(
        candidates,
        key=lambda pair: (abs(pair[1] - pair[0]), pair[0], pair[1]),
    )
    return [(clauses[left], clauses[right]) for left, right in ordered[:max_pairs]]


def _pair_block(
    candidates: list[tuple[ClauseInput, ClauseInput]],
    *,
    offset: int,
) -> str:
    """构造冲突模板中的候选条款对文本。"""
    blocks: list[str] = []
    for local_index, (source, target) in enumerate(
        candidates,
        start=offset,
    ):
        blocks.append(
            "\n".join(
                (
                    f"pair_index={local_index}",
                    f"source={source.get('label', '')}: {source.get('text', '')}",
                    f"target={target.get('label', '')}: {target.get('text', '')}",
                )
            )
        )
    return "\n\n".join(blocks)


def conflict_node(state: AuditState, deps: GraphDeps) -> dict[str, Any]:
    """先规则粗筛，再按批执行矛盾精判。"""
    stage = AUDIT_STAGES[3]
    started = time.perf_counter()
    clauses, budget_warnings = _bounded_clauses(state, deps)
    if not clauses:
        warning = "无条款，跳过矛盾分析"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"conflicts": [], "llm_calls": 0},
        )

    candidates = build_conflict_candidates(clauses, deps.max_conflict_pairs)
    warnings = list(budget_warnings)
    if deps.max_conflict_pairs <= 0:
        warnings.append("冲突候选上限非正数，已跳过候选生成")
    if not candidates:
        return _finish_stage(
            state,
            stage,
            "OK",
            started,
            "无可疑条款对",
            warnings=warnings,
            extra={"conflicts": [], "llm_calls": 0},
        )

    prompt = _resolve_prompt(deps, "conflict")
    if prompt is None:
        warning = "缺少 conflict 模板"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*warnings, warning],
            extra={"conflicts": [], "llm_calls": 0},
        )

    try:
        model = _resolve_llm(deps, "conflict")
    except Exception as exc:  # noqa: BLE001 - 节点契约要求永不抛异常
        detail = f"{_ERR_LLM_UNAVAILABLE}: 矛盾模型不可用：{type(exc).__name__}"
        return _finish_stage(
            state,
            stage,
            "FAILED",
            started,
            detail,
            warnings=[*warnings, detail],
            extra={"conflicts": [], "llm_calls": 0},
        )

    contract_type = state.get("contract_type")
    contract_type_value = (
        contract_type.value if isinstance(contract_type, ContractType) else ""
    )
    trace_id = str(state.get("trace_id", "") or "-")
    contract_id = str(state.get("contract_id", ""))
    batch_size = max(1, deps.conflict_batch_size)
    conflicts: list[CrossClauseConflict] = []
    seen_pairs: set[int] = set()
    llm_calls = 0
    failed_batches = 0
    degraded = False
    conflict_ordinal = 0

    batches = [
        candidates[index : index + batch_size]
        for index in range(0, len(candidates), batch_size)
    ]
    for batch_index, batch in enumerate(batches):
        offset = batch_index * batch_size
        try:
            rendered = prompt.render(
                pair_block=_pair_block(batch, offset=offset),
                contract_type=contract_type_value,
            )
            llm_calls += 1
            value, invoke_warnings = invoke_structured(
                model,
                _ConflictBatch,
                [HumanMessage(content=rendered)],
                trace_id=trace_id,
            )
            warnings.extend(invoke_warnings)
            if value is None:
                failed_batches += 1
                degraded = True
                warnings.append(
                    f"{_ERR_LLM_INVALID_CONFLICT}: 冲突批次 {batch_index + 1} 精判降级"
                )
                continue
        except Exception as exc:  # noqa: BLE001 - 单批失败不得中断其它批次
            failed_batches += 1
            degraded = True
            warnings.append(
                f"{_ERR_LLM_INVALID_CONFLICT}: 冲突批次 {batch_index + 1} 失败：{type(exc).__name__}"
            )
            continue

        for draft in value.conflicts:
            if draft.pair_index in seen_pairs:
                warnings.append(f"冲突 pair_index={draft.pair_index} 重复，已忽略")
                degraded = True
                continue
            if draft.pair_index < 0 or draft.pair_index >= len(candidates):
                warnings.append(f"冲突 pair_index={draft.pair_index} 越界，已忽略")
                degraded = True
                continue

            seen_pairs.add(draft.pair_index)
            source, target = candidates[draft.pair_index]
            conflict_ordinal += 1
            conflicts.append(
                CrossClauseConflict(
                    id=f"{contract_id}:conflict:{conflict_ordinal}",
                    sourceClauseId=source["clause_id"],
                    targetClauseId=target["clause_id"],
                    conflictType=draft.conflict_type,
                    description=draft.description,
                    level=draft.level,
                    suggestion=draft.suggestion,
                    citations=[],
                )
            )

    if batches and failed_batches == len(batches):
        status = "FAILED"
        warnings.append(
            f"{_ERR_LLM_ALL_CONFLICTS_FAILED}: {failed_batches} 个冲突批次均失败"
        )
    else:
        status = "DEGRADED" if degraded else "OK"

    return _finish_stage(
        state,
        stage,
        status,
        started,
        f"矛盾分析完成：{len(conflicts)} 条矛盾",
        warnings=warnings,
        extra={"conflicts": conflicts, "llm_calls": llm_calls},
    )


def standard_compare_node(state: AuditState, deps: GraphDeps) -> dict[str, Any]:
    """逐条款检测是否存在可对照的标准条款。"""
    stage = AUDIT_STAGES[4]
    started = time.perf_counter()
    clauses, budget_warnings = _bounded_clauses(state, deps)
    if not clauses:
        warning = "无条款，跳过标准比对"
        return _finish_stage(
            state,
            stage,
            "SKIPPED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"missing_clauses": [], "rag_calls": 0},
        )

    if deps.rag_tool is None:
        warning = "RAG 工具不可用，无法判定缺失标准对照"
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            warning,
            warnings=[*budget_warnings, warning],
            extra={"missing_clauses": [], "rag_calls": 0},
        )

    contract_type = state.get("contract_type")
    trace_id = str(state.get("trace_id", "") or "-")
    contract_id = str(state.get("contract_id", ""))
    missing_clauses: list[dict[str, Any]] = []
    warnings = list(budget_warnings)
    rag_calls = 0
    degraded = False
    missing_ordinal = 0

    for clause in clauses:
        rag_calls += 1
        try:
            rag_result = deps.rag_tool(
                query=clause["text"],
                contract_type=contract_type,
                source_types=[
                    SourceType.STANDARD_CLAUSE,
                    SourceType.LAW_ARTICLE,
                ],
                top_k=3,
                reason="standard_compare",
                trace_id=trace_id,
            )
        except Exception as exc:  # noqa: BLE001 - 单条款失败不得伪装成缺失
            degraded = True
            warnings.append(
                f"条款 {clause['clause_id']} 标准比对失败：{type(exc).__name__}"
            )
            continue

        if rag_result.status == "EMPTY":
            missing_ordinal += 1
            title = clause.get("label", "") or clause["clause_id"]
            missing_clauses.append(
                {
                    "id": f"{contract_id}:missing:{missing_ordinal}",
                    "standardClauseId": "",
                    "title": title,
                    "reason": rag_result.context_block or EMPTY_CONTEXT_HINT,
                    "suggestion": f"补充与“{title}”对应的标准条款",
                    "canAutoFill": False,
                }
            )
        elif not rag_result.citations or rag_result.status == "DEGRADED":
            degraded = True
            warnings.append(
                f"条款 {clause['clause_id']} 标准比对结果不完整，无法判定缺失"
            )

    return _finish_stage(
        state,
        stage,
        "DEGRADED" if degraded else "OK",
        started,
        f"标准比对完成：{len(missing_clauses)} 条缺失",
        warnings=warnings,
        extra={
            "missing_clauses": missing_clauses,
            "rag_calls": rag_calls,
        },
    )


def _clamp_score(value: int) -> int:
    """把健康分限制在 [0, 100]。"""
    return max(0, min(100, value))


def compute_health_score(
    risks: list[EnhancedRiskItem],
    conflicts: list[CrossClauseConflict],
    missing_clauses: list[dict],
) -> dict[str, Any]:
    """按父文档 §5.5 的唯一公式计算健康分。"""
    compliance_deduction = 0
    performance_deduction = 0

    for risk in risks:
        deduction = _RISK_DEDUCTION[risk.level]
        if risk.category in _PERFORMANCE_RISK_CATEGORIES:
            performance_deduction += deduction
        else:
            compliance_deduction += deduction

    for conflict in conflicts:
        deduction = _CONFLICT_DEDUCTION[conflict.level]
        if conflict.conflictType in _PERFORMANCE_CONFLICT_TYPES:
            performance_deduction += deduction
        else:
            compliance_deduction += deduction

    compliance = _clamp_score(100 - compliance_deduction)
    performance_risk = _clamp_score(100 - performance_deduction)
    clause_completeness = _clamp_score(100 - 3 * len(missing_clauses))
    overall = round(
        0.4 * compliance + 0.4 * performance_risk + 0.2 * clause_completeness
    )
    return {
        "overall": overall,
        "dimensions": {
            "compliance": compliance,
            "performanceRisk": performance_risk,
            "clauseCompleteness": clause_completeness,
        },
    }


def build_report_draft(
    risks: list[EnhancedRiskItem],
    conflicts: list[CrossClauseConflict],
    missing_clauses: list[dict],
    health_score: dict[str, Any],
) -> dict[str, Any]:
    """构造确定性的可序列化报告草稿。"""
    risk_counts = {
        "HIGH": 0,
        "MEDIUM": 0,
        "LOW": 0,
    }
    for risk in risks:
        risk_counts[risk.level.value] += 1

    level_rank = {
        RiskLevel.HIGH: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.LOW: 2,
    }
    top_risks = sorted(
        risks,
        key=lambda risk: (level_rank[risk.level], risk.id),
    )[:5]

    return {
        "summary": (
            f"共识别 {len(risks)} 条风险、{len(conflicts)} 条矛盾、"
            f"{len(missing_clauses)} 条缺失标准对照。"
        ),
        "risk_counts": risk_counts,
        "top_risks": [
            risk.model_dump(mode="json", by_alias=True) for risk in top_risks
        ],
        "health_score": health_score,
    }


def report_draft_node(state: AuditState, deps: GraphDeps) -> dict[str, Any]:
    """纯聚合报告草稿，不调用 LLM / RAG。"""
    stage = AUDIT_STAGES[5]
    started = time.perf_counter()
    risks = list(state.get("risks", []))
    conflicts = list(state.get("conflicts", []))
    missing_clauses = list(state.get("missing_clauses", []))

    try:
        health_score = compute_health_score(risks, conflicts, missing_clauses)
        report_draft = build_report_draft(
            risks,
            conflicts,
            missing_clauses,
            health_score,
        )
        return _finish_stage(
            state,
            stage,
            "OK",
            started,
            f"报告草稿完成：健康分 {health_score['overall']}",
            extra={
                "report_draft": report_draft,
                "health_score": health_score["overall"],
            },
        )
    except Exception as exc:  # noqa: BLE001 - 节点契约要求永不抛异常
        detail = f"报告聚合失败：{type(exc).__name__}"
        empty_score = {
            "overall": 100,
            "dimensions": {
                "compliance": 100,
                "performanceRisk": 100,
                "clauseCompleteness": 100,
            },
        }
        return _finish_stage(
            state,
            stage,
            "DEGRADED",
            started,
            detail,
            warnings=[detail],
            extra={
                "report_draft": build_report_draft([], [], [], empty_score),
                "health_score": 100,
            },
        )


__all__ = [
    "build_report_draft",
    "classify_node",
    "compute_health_score",
    "conflict_node",
    "extract_node",
    "report_draft_node",
    "risk_node",
    "standard_compare_node",
]
