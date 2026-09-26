"""RAG Tool 与三道防线。

设计要点：
1. 只包装 `RAGService`，不实现 embed / recall / fuse / rerank。
2. 超长截断、空召回映射和超时重试统一收敛到本模块。
3. 运行期不抛异常，失败返回合法的 `AgentRAGToolResult(status="EMPTY")`。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from cg_observability import get_logger, get_trace_id, log_event
from rag_contracts.agent_tool import (
    EMPTY_CONTEXT_HINT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    AgentRAGToolInput,
    AgentRAGToolResult,
    build_function_calling_spec,
)
from rag_contracts.enums import ContractType, FallbackReason, SourceType
from rag_contracts.query import (
    RAGQueryRequest,
    RAGQueryResponse,
    RetrievalFilters,
    TokenUsage,
)
from rag_core.config import RagSettings, get_settings
from rag_core.factory import build_service_from_settings
from rag_core.service import RAGService

_LOG = get_logger("ai_engine.agents.tools")
_RETRYABLE_REASONS = {
    FallbackReason.TIMEOUT,
    FallbackReason.STORE_UNAVAILABLE,
}
_TRUNCATION_MARKER_HEAD = "……（中间省略 "
_TRUNCATION_MARKER_TAIL = " 字符）……"


def _contract_query_max_chars() -> int:
    """从冻结 Tool 入参模型读取 query 的 maxLength。"""
    for constraint in AgentRAGToolInput.model_fields["query"].metadata:
        max_length = getattr(constraint, "max_length", None)
        if isinstance(max_length, int):
            return max_length
    raise RuntimeError("AgentRAGToolInput.query 缺少 max_length 约束")


_CONTRACT_QUERY_MAX_CHARS = _contract_query_max_chars()


@dataclass(frozen=True, slots=True)
class TruncationResult:
    """超长查询截断結果。"""

    query: str
    truncated: bool
    omitted_chars: int


def _marker(omitted_chars: int) -> str:
    return f"{_TRUNCATION_MARKER_HEAD}{omitted_chars}{_TRUNCATION_MARKER_TAIL}"


def truncate_query(query: str, max_chars: int) -> TruncationResult:
    """按头 + 省略标记 + 尾截断查询。"""
    if len(query) <= max_chars:
        return TruncationResult(query=query, truncated=False, omitted_chars=0)
    if max_chars <= 0:
        return TruncationResult(
            query="",
            truncated=True,
            omitted_chars=len(query),
        )

    omitted = max(1, len(query) - max_chars)
    for _ in range(4):
        marker = _marker(omitted)
        tail_budget = min(200, max_chars // 5)
        head_budget = max_chars - tail_budget - len(marker)
        if head_budget < 1:
            break
        truncated_query = f"{query[:head_budget]}{marker}{query[-tail_budget:]}"
        actual_omitted = len(query) - head_budget - tail_budget
        if actual_omitted == omitted:
            return TruncationResult(
                query=truncated_query,
                truncated=True,
                omitted_chars=actual_omitted,
            )
        omitted = actual_omitted

    # 上限小到放不下省略标记时，只能保证契约长度，不伪造尾部。
    shortened = query[:max_chars]
    return TruncationResult(
        query=shortened,
        truncated=True,
        omitted_chars=len(query) - len(shortened),
    )


def _empty_result(
    *,
    trace_id: str,
    took_ms: int,
    warnings: list[str],
    fallback_reason: FallbackReason,
    token_usage: TokenUsage | None = None,
) -> AgentRAGToolResult:
    return AgentRAGToolResult(
        status="EMPTY",
        context_block=EMPTY_CONTEXT_HINT,
        citations=[],
        trace_id=trace_id,
        took_ms=took_ms,
        token_usage=token_usage or TokenUsage(),
        warnings=warnings,
        fallback_reason=fallback_reason,
    )


class RagTool:
    """RAG 工具：唯一检索入口与三道防线。"""

    def __init__(
        self,
        service: RAGService | None = None,
        settings: RagSettings | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._service = service or build_service_from_settings(self._settings)
        self._sleeper = sleeper

    @property
    def spec(self) -> dict[str, Any]:
        """返回冻结契约生成器产出的 function-calling 定义。"""
        return build_function_calling_spec()

    def as_langchain_tool(self) -> BaseTool:
        """包装为 LangChain StructuredTool。"""

        def _invoke(**kwargs: Any) -> str:
            return self(**kwargs).to_tool_message()

        return StructuredTool.from_function(
            func=_invoke,
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            args_schema=AgentRAGToolInput,
        )

    def __call__(
        self,
        query: str,
        *,
        contract_type: ContractType | None = None,
        source_types: list[SourceType] | None = None,
        top_k: int = 5,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> AgentRAGToolResult:
        """执行检索，并把全部失败收敛为 EMPTY。"""
        started = time.perf_counter()
        resolved_trace_id = trace_id or get_trace_id() or "-"
        warnings: list[str] = []
        accumulated_usage = TokenUsage()

        clamped_top_k = max(1, min(20, int(top_k)))
        if clamped_top_k != top_k:
            warnings.append(f"top_k={top_k} 已夹取到 {clamped_top_k}")

        max_chars = min(self._settings.max_query_chars, _CONTRACT_QUERY_MAX_CHARS)
        truncated = truncate_query(query, max_chars)
        if truncated.truncated:
            warnings.append(f"OVERLONG_QUERY: 已截断 {truncated.omitted_chars} 字符")
            log_event(
                _LOG,
                "rag_tool.truncate",
                level="WARNING",
                trace_id=resolved_trace_id,
                omitted_chars=truncated.omitted_chars,
                max_chars=max_chars,
            )

        if not truncated.query:
            return _empty_result(
                trace_id=resolved_trace_id,
                took_ms=int((time.perf_counter() - started) * 1000),
                warnings=[*warnings, "query 为空，无法检索"],
                fallback_reason=FallbackReason.EMPTY_RECALL,
            )

        request = RAGQueryRequest(
            trace_id=resolved_trace_id,
            query=truncated.query,
            top_k=clamped_top_k,
            score_threshold=self._settings.score_threshold,
            max_context_tokens=self._settings.max_context_tokens,
            timeout_ms=self._settings.timeout_ms,
            rerank_top_n=self._settings.rerank_top_n,
            enable_rerank=self._settings.rerank_enabled,
            filters=RetrievalFilters(
                source_types=source_types,
                contract_type=contract_type,
            ),
        )

        max_retries = max(0, self._settings.max_retries)
        total_attempts = max_retries + 1
        last_reason = FallbackReason.STORE_UNAVAILABLE

        for attempt_index in range(total_attempts):
            try:
                response = self._service.retrieve(request)
            except Exception as exc:  # noqa: BLE001 - 工具契约要求永不抛异常
                last_reason = FallbackReason.STORE_UNAVAILABLE
                warnings.append(f"RAG 调用失败：{type(exc).__name__}: {str(exc)[:200]}")
                log_event(
                    _LOG,
                    "rag_tool.degrade",
                    level="WARNING",
                    trace_id=resolved_trace_id,
                    reason=reason or "",
                    error_type=type(exc).__name__,
                    attempt=attempt_index + 1,
                )
                if attempt_index < max_retries:
                    delay = min(0.2 * (2**attempt_index), 2.0)
                    log_event(
                        _LOG,
                        "rag_tool.retry",
                        level="WARNING",
                        trace_id=resolved_trace_id,
                        attempt=attempt_index + 1,
                        delay_s=delay,
                    )
                    self._sleeper(delay)
                    continue
                break

            _accumulate_usage(accumulated_usage, response.token_usage)
            retryable = response.fallback_reason in _RETRYABLE_REASONS
            if retryable and attempt_index < max_retries:
                last_reason = response.fallback_reason
                warnings.extend(response.warnings)
                delay = min(0.2 * (2**attempt_index), 2.0)
                log_event(
                    _LOG,
                    "rag_tool.retry",
                    level="WARNING",
                    trace_id=resolved_trace_id,
                    attempt=attempt_index + 1,
                    delay_s=delay,
                    fallback_reason=response.fallback_reason.value,
                )
                self._sleeper(delay)
                continue

            return self._map_response(
                response,
                trace_id=resolved_trace_id,
                took_ms=int((time.perf_counter() - started) * 1000),
                warnings=[*warnings, *response.warnings],
                token_usage=accumulated_usage,
            )

        return _empty_result(
            trace_id=resolved_trace_id,
            took_ms=int((time.perf_counter() - started) * 1000),
            warnings=warnings,
            fallback_reason=last_reason,
            token_usage=accumulated_usage,
        )

    @staticmethod
    def _map_response(
        response: RAGQueryResponse,
        *,
        trace_id: str,
        took_ms: int,
        warnings: list[str],
        token_usage: TokenUsage,
    ) -> AgentRAGToolResult:
        if not response.retrieved_docs:
            return _empty_result(
                trace_id=trace_id,
                took_ms=took_ms,
                warnings=warnings,
                fallback_reason=response.fallback_reason,
                token_usage=token_usage,
            )
        return AgentRAGToolResult(
            status="DEGRADED" if response.degraded else "OK",
            context_block=response.context_block,
            citations=[doc.citation for doc in response.retrieved_docs],
            trace_id=trace_id,
            took_ms=took_ms,
            token_usage=token_usage,
            warnings=warnings,
            fallback_reason=response.fallback_reason,
        )


def _accumulate_usage(total: TokenUsage, current: TokenUsage) -> None:
    """累加重试期间各次响应的 Token 统计。"""
    total.add(
        embedding_tokens=current.embedding_tokens,
        rerank_tokens=current.rerank_tokens,
        prompt_tokens=current.prompt_tokens,
        completion_tokens=current.completion_tokens,
    )
    total.estimated = total.estimated and current.estimated


def build_rag_tool(
    service: RAGService | None = None,
    settings: RagSettings | None = None,
) -> RagTool:
    """构造 RAG Tool；未传 service 时走 `RagSettings` 工厂。"""
    return RagTool(service=service, settings=settings)


__all__ = ["RagTool", "TruncationResult", "build_rag_tool", "truncate_query"]
