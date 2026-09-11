"""RAG 检索服务编排（设计文档 §2.4 / §3.1.1）。

职责：embed → dense（+sparse）→ fuse → rerank → 阈值 → 拼装 → 降级 → Token 统计。
**只依赖 `rag_contracts.ports` 的 Protocol**，不 import 任何具体 SDK。

Phase 1 只落地 dense 单路；sparse / RRF 融合在 T03 接入，
届时只需给本类传入 `sparse_store` 并在 `_fuse()` 中接 `rag_core.fusion`。

得分口径（重要）：
- `dense` 保存**原始余弦相似度** [-1,1]，供调试；
- `final` 为 [0,1]：有 rerank 用 rerank 分，否则用 clamp 后的余弦分。
  ⚠️ 这里用 clamp 而非 `(x+1)/2` 映射：后者会把"无关文本"（余弦≈0）抬到 0.5，
  直接击穿设计文档锁定的 0.35 阈值，使"缺失标准对照"信号失效。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Sequence

from cg_observability import TokenCounter, TokenMeter, TraceContext, get_logger, log_event
from pydantic import ValidationError
from rag_contracts.agent_tool import EMPTY_CONTEXT_HINT
from rag_contracts.chunk import ContractChunk
from rag_contracts.citation import clamp_score, to_citation
from rag_contracts.enums import FallbackReason, RetrieveStrategy
from rag_contracts.errors import RagError, StoreUnavailableError
from rag_contracts.ports import (
    EmbedderPort,
    RerankerPort,
    SparseStorePort,
    VectorHit,
    VectorStorePort,
)
from rag_contracts.query import (
    ChunkScores,
    RAGQueryRequest,
    RAGQueryResponse,
    RetrievedDoc,
    TokenUsage,
)

from .context_builder import ContextBuilder

_LOG = get_logger("rag_core.service")


class RAGService:
    """检索编排服务。

    Attributes:
        embedder: 向量化端口。
        vector_store: 稠密向量库端口。
        sparse_store: 稀疏检索端口（Phase 2，当前可为 None）。
        reranker: 重排端口（Phase 1 为 NoopReranker）。
    """

    def __init__(
        self,
        *,
        embedder: EmbedderPort,
        vector_store: VectorStorePort,
        counter: TokenCounter | None = None,
        reranker: RerankerPort | None = None,
        sparse_store: SparseStorePort | None = None,
        dense_top: int = 20,
        default_max_context_tokens: int = 3000,
        logger: Any = None,
    ) -> None:
        """初始化（依赖全部由外部注入，便于单测与 Phase 切换）。

        Args:
            embedder: 向量化实现。
            vector_store: 向量库实现。
            counter: Token 计数器；为空则自建。
            reranker: 重排实现；为空则跳过重排（用 dense 分）。
            sparse_store: BM25 实现；当前版本未接入，预留给 Phase 2。
            dense_top: dense 路召回条数（融合前的候选量）。
            default_max_context_tokens: 请求未指定时的上下文预算。
            logger: 自定义 logger；为空用模块级 logger。
        """
        self._embedder: EmbedderPort = embedder
        self._vector_store: VectorStorePort = vector_store
        self._sparse_store: SparseStorePort | None = sparse_store
        self._reranker: RerankerPort | None = reranker
        self._counter: TokenCounter = counter or TokenCounter()
        self._builder: ContextBuilder = ContextBuilder(counter=self._counter)
        self._dense_top: int = max(1, int(dense_top))
        self._default_max_context_tokens: int = int(default_max_context_tokens)
        self._logger: Any = logger or _LOG

    # ---------------- 只读属性（供建库脚本复用同一套组件） ----------------
    @property
    def embedder(self) -> EmbedderPort:
        """向量化实现。"""
        return self._embedder

    @property
    def vector_store(self) -> VectorStorePort:
        """向量库实现。"""
        return self._vector_store

    @property
    def sparse_store(self) -> SparseStorePort | None:
        """稀疏检索实现（Phase 2）。"""
        return self._sparse_store

    @property
    def reranker(self) -> RerankerPort | None:
        """重排实现。"""
        return self._reranker

    @property
    def counter(self) -> TokenCounter:
        """Token 计数器。"""
        return self._counter

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def retrieve(self, request: RAGQueryRequest) -> RAGQueryResponse:
        """执行一次检索。

        Args:
            request: 检索请求契约。

        Returns:
            检索响应契约（永不抛异常：任何失败都转为降级响应）。
        """
        started = time.perf_counter()
        meter = TokenMeter(self._counter)
        warnings: list[str] = []
        strategy = request.strategy

        # Phase 1：没有 BM25 实现时，HYBRID_RRF 自动退化为 DENSE_ONLY
        if strategy is not RetrieveStrategy.DENSE_ONLY and self._sparse_store is None:
            warnings.append("未接入 BM25（Phase 2），策略已退化为 DENSE_ONLY")
            strategy = RetrieveStrategy.DENSE_ONLY

        with TraceContext(trace_id=request.trace_id, name="rag.retrieve") as ctx:
            trace_id = ctx.trace_id
            log_event(
                self._logger,
                "retrieve.start",
                trace_id=trace_id,
                query_len=len(request.query),
                query_hash=hashlib.sha1(request.query.encode("utf-8")).hexdigest()[:8],
                top_k=request.top_k,
                threshold=request.score_threshold,
                strategy=strategy.value,
                has_filters=request.filters.model_dump(exclude_none=True) != {},
            )

            try:
                candidates = self._recall(request, meter, warnings, strategy)
            except RagError as exc:
                log_event(
                    self._logger,
                    "retrieve.degrade",
                    level="WARNING",
                    trace_id=trace_id,
                    fallback_reason="STORE_UNAVAILABLE",
                    detail=str(exc),
                    error_code=exc.code,
                )
                return self._degraded(
                    request,
                    reason=FallbackReason.STORE_UNAVAILABLE,
                    detail=str(exc),
                    started=started,
                    meter=meter,
                    warnings=warnings + [f"检索失败已降级：{exc.code}"],
                )
            except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都转降级，绝不向上抛
                log_event(
                    self._logger,
                    "retrieve.degrade",
                    level="ERROR",
                    trace_id=trace_id,
                    fallback_reason="STORE_UNAVAILABLE",
                    detail=repr(exc),
                )
                return self._degraded(
                    request,
                    reason=FallbackReason.STORE_UNAVAILABLE,
                    detail=repr(exc),
                    started=started,
                    meter=meter,
                    warnings=warnings + [f"检索异常已降级：{type(exc).__name__}"],
                )

            docs = self._rank(request, candidates, meter, warnings, trace_id)

            # ---- 空召回 / 低于阈值 → 降级（缺失检测信号，不是错误） ----
            degraded = False
            reason = FallbackReason.NONE
            if not docs:
                degraded = True
                reason = FallbackReason.EMPTY_RECALL
                log_event(
                    self._logger,
                    "retrieve.degrade",
                    level="WARNING",
                    trace_id=trace_id,
                    fallback_reason=reason.value,
                    detail=f"候选 {len(candidates)} 条，过滤后 0 条（阈值 {request.score_threshold}）",
                )

            # ---- 上下文拼装 ----
            budget = request.max_context_tokens or self._default_max_context_tokens
            with meter.stage("build"):
                built = self._builder.build(docs, budget)
            log_event(
                self._logger,
                "context.build",
                trace_id=trace_id,
                injected_chunks=built.injected_chunks,
                context_tokens=built.context_tokens,
                budget=budget,
                truncated=built.truncated,
                prompt_preview=built.context_block[:500],
            )

            usage = TokenUsage(
                embedding_tokens=meter.usage("embedding"),
                prompt_tokens=built.context_tokens,
                estimated=self._counter.estimated,
            )
            usage.add()  # 重算 total_tokens

            took_ms = int((time.perf_counter() - started) * 1000)
            response = RAGQueryResponse(
                trace_id=trace_id,
                query=request.query,
                strategy=strategy,
                retrieved_docs=docs,
                total_candidates=len(candidates),
                context_block=(
                    EMPTY_CONTEXT_HINT if degraded and not docs else built.context_block
                ),
                context_tokens=built.context_tokens,
                truncated=built.truncated,
                degraded=degraded,
                fallback_reason=reason,
                warnings=warnings,
                token_usage=usage,
                stage_latency_ms=meter.latencies,
                took_ms=took_ms,
            )

            log_event(
                self._logger,
                "retrieve.done",
                trace_id=trace_id,
                query_len=len(request.query),
                candidates=len(candidates),
                returned=len(docs),
                top_score=round(docs[0].scores.final, 4) if docs else 0.0,
                token_count=usage.total_tokens,
                context_tokens=built.context_tokens,
                degraded=degraded,
                took_ms=took_ms,
            )
            return response

    # ------------------------------------------------------------------
    # 阶段：召回
    # ------------------------------------------------------------------
    def _recall(
        self,
        request: RAGQueryRequest,
        meter: TokenMeter,
        warnings: list[str],
        strategy: RetrieveStrategy,
    ) -> list[tuple[ContractChunk, float]]:
        """向量化 + 稠密召回，返回 (切片, 原始余弦分) 列表。

        Args:
            request: 检索请求。
            meter: Token/耗时计量器。
            warnings: 告警收集列表（原地追加）。
            strategy: 生效策略。

        Returns:
            候选列表（按余弦降序）。

        Raises:
            StoreUnavailableError: 向量库不可用或向量化失败。
        """
        try:
            with meter.stage("embed"):
                vector = self._embedder.embed_one(request.query)
            meter.count_text("embedding", request.query)
        except Exception as exc:  # noqa: BLE001 - 向量化失败统一收敛
            raise StoreUnavailableError(f"向量化失败：{exc}") from exc

        limit = max(self._dense_top, request.top_k)
        with meter.stage("dense"):
            hits: list[VectorHit] = self._vector_store.search(
                vector, limit, request.filters
            )
        log_event(
            self._logger,
            "retrieve.dense",
            trace_id=request.trace_id,
            candidates=len(hits),
            top_score=round(hits[0].score, 4) if hits else 0.0,
            latency_ms=meter.latency_ms("dense"),
            strategy=strategy.value,
        )

        candidates: list[tuple[ContractChunk, float]] = []
        invalid = 0
        for hit in hits:
            try:
                chunk = ContractChunk.from_payload(hit.payload)
            except ValidationError:
                invalid += 1
                continue
            candidates.append((chunk, float(hit.score)))
        if invalid:
            warnings.append(f"{invalid} 条候选 payload 未通过契约校验，已丢弃")
        return candidates

    # ------------------------------------------------------------------
    # 阶段：重排 + 阈值 + 截断
    # ------------------------------------------------------------------
    def _rank(
        self,
        request: RAGQueryRequest,
        candidates: Sequence[tuple[ContractChunk, float]],
        meter: TokenMeter,
        warnings: list[str],
        trace_id: str,
    ) -> list[RetrievedDoc]:
        """重排 → 阈值过滤 → top_n/top_k 截断，产出最终文档列表。

        Args:
            request: 检索请求。
            candidates: 候选（切片, 余弦分）。
            meter: 计量器。
            warnings: 告警收集列表。
            trace_id: 追踪 ID。

        Returns:
            已排序的 RetrievedDoc 列表。
        """
        if not candidates:
            return []

        texts = [chunk.text for chunk, _ in candidates]
        base_scores = [clamp_score(score) for _, score in candidates]

        rerank_scores: list[float] | None = None
        if request.enable_rerank and self._reranker is not None:
            with meter.stage("rerank"):
                rerank_scores = self._rerank_safe(request.query, texts, base_scores)
            if len(rerank_scores) == len(texts):
                dropped = sum(
                    1 for s in rerank_scores if s < request.score_threshold
                )
                log_event(
                    self._logger,
                    "retrieve.rerank",
                    trace_id=trace_id,
                    model=getattr(self._reranker, "model_name", "unknown"),
                    pairs=len(texts),
                    score_max=round(max(rerank_scores), 4),
                    score_min=round(min(rerank_scores), 4),
                    score_mean=round(sum(rerank_scores) / len(rerank_scores), 4),
                    dropped_count=dropped,
                    latency_ms=meter.latency_ms("rerank"),
                )
            else:
                warnings.append("重排返回长度不符，已回退为融合分")
                rerank_scores = None

        final_scores = rerank_scores if rerank_scores is not None else base_scores
        ordered = sorted(
            zip(candidates, final_scores), key=lambda item: item[1], reverse=True
        )

        # 重排后保留条数（设计文档：3~5 为宜，过多挤占 Prompt 预算）
        if request.enable_rerank and rerank_scores is not None:
            ordered = ordered[: request.rerank_top_n]
        ordered = ordered[: request.top_k]

        docs: list[RetrievedDoc] = []
        for rank, ((chunk, dense_score), final) in enumerate(ordered, start=1):
            if final < request.score_threshold:
                continue
            docs.append(
                RetrievedDoc(
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    source_type=chunk.source_type,
                    chunk_type=chunk.chunk_type,
                    section_id=chunk.section_id,
                    law_code=chunk.law_code,
                    article_no=chunk.article_no,
                    title=chunk.title,
                    text=chunk.text,
                    page_no=chunk.page_no,
                    bbox=chunk.bbox,
                    contract_type=chunk.contract_type,
                    category=chunk.category,
                    section_path=chunk.section_path,
                    token_count=chunk.token_count,
                    scores=ChunkScores(
                        dense=round(dense_score, 6),
                        fused=None,
                        rerank=(round(final, 6) if rerank_scores is not None else None),
                        final=round(final, 6),
                        rank=rank,
                    ),
                    citation=to_citation(chunk, final),
                )
            )
        return docs

    def _rerank_safe(
        self, query: str, texts: Sequence[str], base_scores: Sequence[float]
    ) -> list[float]:
        """调用重排器（三参契约）。

        自 C1 决策起 `RerankerPort.rerank` 已统一为
        `(query, texts, base_scores=None)`，故此处不再需要
        `try/except TypeError` 做签名兼容兜底。

        Args:
            query: 查询文本。
            texts: 候选文本。
            base_scores: 上游得分。

        Returns:
            重排分列表。
        """
        assert self._reranker is not None
        return list(
            self._reranker.rerank(query, list(texts), base_scores=list(base_scores))
        )

    # ------------------------------------------------------------------
    # 降级响应
    # ------------------------------------------------------------------
    def _degraded(
        self,
        request: RAGQueryRequest,
        *,
        reason: FallbackReason,
        detail: str,
        started: float,
        meter: TokenMeter,
        warnings: list[str],
    ) -> RAGQueryResponse:
        """构造降级响应（retrieved_docs 为空，context_block 为兜底提示语）。

        Args:
            request: 原始请求。
            reason: 降级原因。
            detail: 详细说明（写入 warnings）。
            started: 起始 perf_counter。
            meter: 计量器。
            warnings: 已有告警。

        Returns:
            降级后的 RAGQueryResponse。
        """
        usage = TokenUsage(
            embedding_tokens=meter.usage("embedding"),
            estimated=self._counter.estimated,
        )
        usage.add()
        return RAGQueryResponse(
            trace_id=request.trace_id,
            query=request.query,
            strategy=request.strategy,
            retrieved_docs=[],
            total_candidates=0,
            context_block=EMPTY_CONTEXT_HINT,
            context_tokens=self._counter.count(EMPTY_CONTEXT_HINT),
            truncated=False,
            degraded=True,
            fallback_reason=reason,
            warnings=warnings + [f"降级原因：{reason.value}；{detail}"],
            token_usage=usage,
            stage_latency_ms=meter.latencies,
            took_ms=int((time.perf_counter() - started) * 1000),
        )


__all__ = ["RAGService"]
