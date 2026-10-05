"""RAG Tool 三道防线单测（T-B4 / C9）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_engine.agents.tools import build_rag_tool, truncate_query
from rag_contracts.agent_tool import EMPTY_CONTEXT_HINT, TOOL_NAME
from rag_contracts.enums import (
    ChunkType,
    ContractType,
    FallbackReason,
    SourceType,
)
from rag_contracts.query import (
    ChunkScores,
    CitationOut,
    RAGQueryRequest,
    RAGQueryResponse,
    RetrievedDoc,
)
from rag_core.config import RagSettings


def _settings(**overrides: Any) -> RagSettings:
    defaults = {
        "vector_store": "inmemory",
        "embed_mode": "hash",
        "rerank_mode": "noop",
        "bm25_enabled": False,
        "timeout_ms": 1000,
        "max_retries": 2,
    }
    defaults.update(overrides)
    return RagSettings(_env_file=None, **defaults)


def _doc() -> RetrievedDoc:
    return RetrievedDoc(
        chunk_id="chunk-1",
        doc_id="doc-1",
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id="1",
        text="标准条款正文",
        scores=ChunkScores(final=0.9, rank=1),
        citation=CitationOut(
            sourceType=SourceType.STANDARD_CLAUSE,
            sourceTitle="标准条款",
            contentChunk="标准条款正文",
            relevanceScore=0.9,
        ),
    )


def _response(
    *,
    docs: list[RetrievedDoc] | None = None,
    context_block: str = "上下文",
    degraded: bool = False,
    fallback_reason: FallbackReason = FallbackReason.NONE,
    warnings: list[str] | None = None,
) -> RAGQueryResponse:
    return RAGQueryResponse(
        trace_id="t1",
        query="query",
        retrieved_docs=docs or [],
        context_block=context_block,
        degraded=degraded,
        fallback_reason=fallback_reason,
        warnings=warnings or [],
    )


class _StubService:
    def __init__(self, outcomes: list[RAGQueryResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.requests: list[RAGQueryRequest] = []

    def retrieve(self, request: RAGQueryRequest) -> RAGQueryResponse:
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("stub service 无更多预设结果")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_generic_law_is_retrieved_without_contract_type_and_injected() -> None:
    standard = _doc()
    law = standard.model_copy(
        update={
            "chunk_id": "law-577",
            "doc_id": "civil-code",
            "source_type": SourceType.LAW_ARTICLE,
            "text": "当事人不履行合同义务，应承担违约责任。",
            "citation": standard.citation.model_copy(
                update={
                    "sourceType": SourceType.LAW_ARTICLE,
                    "sourceTitle": "民法典第五百七十七条",
                }
            ),
        }
    )
    tool, service = _tool([_response(docs=[standard]), _response(docs=[law])])
    result = tool(
        "买方逾期付款",
        contract_type=ContractType.PURCHASE,
        source_types=[SourceType.STANDARD_CLAUSE, SourceType.LAW_ARTICLE],
    )
    assert service.requests[0].filters.contract_type == ContractType.PURCHASE
    assert service.requests[0].filters.source_types == [SourceType.STANDARD_CLAUSE]
    assert service.requests[1].filters.contract_type is None
    assert service.requests[1].filters.source_types == [SourceType.LAW_ARTICLE]
    assert {c.sourceType for c in result.citations} == {
        SourceType.STANDARD_CLAUSE,
        SourceType.LAW_ARTICLE,
    }
    assert "标准条款正文" in result.context_block
    assert "当事人不履行合同义务" in result.context_block
    assert result.status == "OK"


def test_law_only_clears_contract_filter() -> None:
    tool, service = _tool([_response(docs=[_doc()])])
    tool(
        "合同违约责任",
        contract_type=ContractType.NDA,
        source_types=[SourceType.LAW_ARTICLE],
    )
    assert len(service.requests) == 1
    assert service.requests[0].filters.contract_type is None


def test_partial_library_is_degraded_but_keeps_available_context() -> None:
    tool, _ = _tool(
        [
            _response(docs=[_doc()]),
            _response(degraded=True, fallback_reason=FallbackReason.EMPTY_RECALL),
        ]
    )
    result = tool(
        "合同违约责任",
        contract_type=ContractType.PURCHASE,
        source_types=[SourceType.STANDARD_CLAUSE, SourceType.LAW_ARTICLE],
    )
    assert result.status == "DEGRADED"
    assert len(result.citations) == 1
    assert "标准条款正文" in result.context_block
    assert any("通用法条未召回" in warning for warning in result.warnings)


def test_combined_context_citations_follow_injected_budget() -> None:
    standard = _doc().model_copy(update={"text": "付款责任" * 100})
    law = standard.model_copy(
        update={"chunk_id": "law", "source_type": SourceType.LAW_ARTICLE}
    )
    tool, _ = _tool(
        [_response(docs=[standard]), _response(docs=[law])],
        settings=_settings(max_context_tokens=256),
    )
    result = tool(
        "合同违约责任",
        contract_type=ContractType.PURCHASE,
        source_types=[SourceType.STANDARD_CLAUSE, SourceType.LAW_ARTICLE],
    )
    assert len(result.citations) == result.context_block.count("[section=")
    assert len(result.citations) == 1


def _tool(
    outcomes: list[RAGQueryResponse | Exception],
    *,
    settings: RagSettings | None = None,
    waits: list[float] | None = None,
) -> tuple[Any, _StubService]:
    service = _StubService(outcomes)
    wait_log = waits if waits is not None else []
    tool = build_rag_tool(
        service=service,  # type: ignore[arg-type]
        settings=settings or _settings(),
    )
    tool._sleeper = wait_log.append
    return tool, service


def test_spec_is_strict_and_self_contained() -> None:
    tool, _ = _tool([])
    spec = tool.spec
    function = spec["function"]
    parameters = function["parameters"]
    assert function["name"] == TOOL_NAME
    assert function["strict"] is True
    assert parameters["additionalProperties"] is False
    assert set(parameters["required"]) == set(parameters["properties"])
    assert "$defs" not in parameters


def test_truncate_query_head_tail() -> None:
    long_result = truncate_query("甲" * 5000, 2000)
    assert long_result.truncated is True
    assert len(long_result.query) <= 2000
    assert long_result.omitted_chars > 0

    exact = truncate_query("甲" * 2000, 2000)
    assert exact.query == "甲" * 2000
    assert exact.truncated is False

    tiny = truncate_query("甲" * 500, 5)
    assert tiny.truncated is True
    assert len(tiny.query) <= 5


def test_empty_recall_maps_to_hint() -> None:
    tool, _ = _tool(
        [
            _response(
                docs=[],
                context_block="",
                degraded=True,
                fallback_reason=FallbackReason.EMPTY_RECALL,
            )
        ]
    )
    result = tool("合同条款")
    assert result.status == "EMPTY"
    assert result.context_block == EMPTY_CONTEXT_HINT
    assert result.citations == []


def test_retry_then_success() -> None:
    waits: list[float] = []
    tool, service = _tool(
        [
            RuntimeError("first"),
            RuntimeError("second"),
            _response(docs=[_doc()]),
        ],
        waits=waits,
    )
    result = tool("合同条款")
    assert len(service.requests) == 3
    assert waits == [0.2, 0.4]
    assert result.status == "OK"
    assert len(result.citations) == 1


def test_timeout_fallback_is_retried() -> None:
    waits: list[float] = []
    tool, service = _tool(
        [
            _response(
                docs=[],
                degraded=True,
                fallback_reason=FallbackReason.TIMEOUT,
            ),
            _response(docs=[_doc()]),
        ],
        waits=waits,
    )
    result = tool("合同条款")
    assert len(service.requests) == 2
    assert waits == [0.2]
    assert result.status == "OK"


def test_retry_exhausted_returns_empty() -> None:
    tool, service = _tool(
        [
            RuntimeError("first"),
            RuntimeError("second"),
            RuntimeError("third"),
        ]
    )
    result = tool("合同条款")
    assert len(service.requests) == 3
    assert result.status == "EMPTY"
    assert result.context_block == EMPTY_CONTEXT_HINT
    assert result.fallback_reason is FallbackReason.STORE_UNAVAILABLE


def test_empty_recall_not_retried() -> None:
    tool, service = _tool(
        [
            _response(
                docs=[],
                degraded=True,
                fallback_reason=FallbackReason.EMPTY_RECALL,
            )
        ]
    )
    tool("合同条款")
    assert len(service.requests) == 1


def test_top_k_clamped_with_warning() -> None:
    tool, service = _tool(
        [
            _response(
                docs=[],
                fallback_reason=FallbackReason.EMPTY_RECALL,
            )
        ]
    )
    result = tool("合同条款", top_k=99)
    assert service.requests[0].top_k == 20
    assert any("top_k=99" in warning for warning in result.warnings)


def test_degraded_with_docs_returns_degraded_status() -> None:
    tool, _ = _tool(
        [
            _response(
                docs=[_doc()],
                context_block="部分降级上下文",
                degraded=True,
                fallback_reason=FallbackReason.RERANK_FAILED,
                warnings=["精排失败"],
            )
        ]
    )
    result = tool("合同条款")
    assert result.status == "DEGRADED"
    assert result.context_block == "部分降级上下文"
    assert result.warnings == ["精排失败"]


def test_as_langchain_tool_name() -> None:
    tool, _ = _tool([])
    assert tool.as_langchain_tool().name == TOOL_NAME


def test_no_sdk_leak_in_source() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "ai_engine" / "agents" / "tools.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("qdrant_client", "pymilvus", "httpx", "import requests"):
        assert forbidden not in source
