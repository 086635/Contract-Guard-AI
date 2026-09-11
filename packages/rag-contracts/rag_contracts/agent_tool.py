"""AgentRAGToolContract：Tool-calling 入参/出参契约（RAG_MODULE_DESIGN.md §2.3）。

三道防线（Phase 3 强制，本模块提供其中的契约与工具描述）：
1. 超长文本拦截：query > max_query_chars 时按"头部+尾部"截断并记 warning。
2. 空召回降级：无命中或最高分 < 阈值 → status=EMPTY，返回固定兜底提示语。
3. 超时重试：整体 timeout_ms 内未返回 → 指数退避重试 N 次，仍失败则降级。

⚠️ OpenAI strict 模式硬约束：
`strict: true` 时 `required` 必须列出**全部**字段（否则 API 直接 400），
字段的可选性由 `["string", "null"]` 这类联合类型表达。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import ContractType, SourceType
from .query import CitationOut, FallbackReason, TokenUsage

# 工具名：与 LangGraph @tool 名称、设计文档保持一致
TOOL_NAME = "retrieve_contract_knowledge"

# 空召回兜底提示语：必须让 LLM 明确知道"没有依据"而不是"依据为空"，否则会幻觉出法条
EMPTY_CONTEXT_HINT = (
    "知识库未检索到可对照的标准条款或法条，请依据通用法律原则评估，"
    "并在结论中明确标注【缺失标准对照】。"
)

# 工具描述：LLM 靠它决定何时调用，需写清"何时不该调用"
TOOL_DESCRIPTION = (
    "从企业标准条款库、《民法典》等法律法规库与裁判文书库中检索与给定条款相关的"
    "标准条款或法条原文。当需要外部法律依据支撑风险判断、条款比对或缺失检测时调用。"
    "返回已精排的条款原文与来源标识；若无匹配会明确提示【缺失标准对照】。"
)


class AgentRAGToolInput(BaseModel):
    """@tool 入参契约。字段名与 JSON Schema 严格一致（strict mode）。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1,
        max_length=2000,
        description="检索文本。优先用待审条款的核心争议句，而非整篇合同。",
    )
    contract_type: ContractType | None = Field(
        default=None,
        description="合同类型，传入可提升同类型标准条款召回优先级。",
    )
    source_types: list[SourceType] | None = Field(
        default=None,
        description="限定检索源：LAW_ARTICLE 查法律依据 / STANDARD_CLAUSE 查企业标准。",
    )
    top_k: int = Field(default=5, ge=1, le=20, description="期望引用条数，建议 3~5。")
    reason: str | None = Field(
        default=None,
        max_length=300,
        description="检索意图自述，仅用于日志归因，不影响检索结果。",
    )


class AgentRAGToolResult(BaseModel):
    """@tool 出参契约。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["OK", "DEGRADED", "EMPTY"] = Field(
        description="OK=正常召回；DEGRADED=部分降级；EMPTY=空召回/低于阈值"
    )
    context_block: str = Field(description="可直接注入 Prompt 的上下文")
    citations: list[CitationOut] = Field(
        default_factory=list, description="可直接赋给 EnhancedRiskItem.citations"
    )
    trace_id: str = Field(default="", description="全链路追踪 ID")
    took_ms: int = Field(default=0, ge=0, description="耗时（毫秒）")
    token_usage: TokenUsage = Field(default_factory=TokenUsage, description="Token 耗用")
    warnings: list[str] = Field(default_factory=list, description="非致命告警")
    fallback_reason: FallbackReason = Field(
        default=FallbackReason.NONE, description="降级原因"
    )

    def to_tool_message(self) -> str:
        """返回给 LLM 的字符串。

        EMPTY 时必须让模型知道"没有依据"而非"依据为空"。

        Returns:
            形如 `[status=OK][trace_id=xxx]\n<context_block>` 的字符串。
        """
        head = f"[status={self.status}][trace_id={self.trace_id}]"
        warn = ("\n[警告] " + "; ".join(self.warnings)) if self.warnings else ""
        return f"{head}\n{self.context_block}{warn}"


def _dereference(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """递归内联 `$ref`（`#/$defs/X`），产出无外部引用的自包含 Schema。

    OpenAI strict 模式对含 `$ref` 的嵌套定义支持不稳定，
    展开后可保证工具定义被任何兼容实现接受。

    Args:
        node: 当前 JSON Schema 节点（dict / list / 标量）。
        defs: 顶层 `$defs` 字典。
        depth: 递归深度，超过 20 层直接返回原节点（防御循环引用）。

    Returns:
        内联后的新节点。
    """
    if depth > 20:
        return node
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1], {})
            merged: dict[str, Any] = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
            return _dereference(merged, defs, depth + 1)
        return {k: _dereference(v, defs, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_dereference(item, defs, depth + 1) for item in node]
    return node


def build_function_calling_spec() -> dict[str, Any]:
    """导出 OpenAI / LangChain 兼容的 function calling 描述（strict 模式）。

    由 Pydantic 模型自动生成，避免手写 JSON 与模型漂移。
    strict=True 要求：所有字段 required 或显式 nullable，additionalProperties=false。

    Returns:
        形如 `{"type": "function", "function": {...}}` 的工具定义字典。
    """
    params = AgentRAGToolInput.model_json_schema(mode="serialization")
    defs: dict[str, Any] = params.pop("$defs", {}) or {}
    params = _dereference(params, defs)
    params["additionalProperties"] = False
    # ⚠️ OpenAI strict 模式硬约束：所有字段必须出现在 required 中，
    # 可选性通过 ["type", "null"] 联合类型表达（Pydantic 已自动生成）。
    # 若不这么做，OpenAI 会直接 400 拒绝该工具定义。
    params["required"] = sorted(params.get("properties", {}).keys())
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "strict": True,
            "parameters": params,
        },
    }


__all__ = [
    "EMPTY_CONTEXT_HINT",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "AgentRAGToolInput",
    "AgentRAGToolResult",
    "build_function_calling_spec",
]
