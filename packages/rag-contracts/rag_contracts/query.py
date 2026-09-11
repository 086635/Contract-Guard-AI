"""RAG 检索请求 / 响应契约（RAG_MODULE_DESIGN.md §2.2）。

设计要点：
1. 请求即配置：所有阈值、策略、预算都在请求里，便于 A/B 与灰度。
2. 响应自带 citation：Agent 无需二次转换即可塞进 EnhancedRiskItem.citations。
3. 响应自带 token_usage 与 stage_latency_ms：可观测性的数据载体。
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .chunk import BBox
from .enums import ChunkType, ContractType, FallbackReason, RetrieveStrategy, SourceType

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
# 沿用前作 RAG_IMPLEMENTATION_GUIDE：RRF 融合常数
DEFAULT_RRF_K: int = 60
# 沿用前作：低于此分判"缺失标准对照"
DEFAULT_SCORE_THRESHOLD: float = 0.35


class RetrievalFilters(BaseModel):
    """检索过滤条件。`None` 表示该维度不限。"""

    model_config = ConfigDict(extra="forbid")

    source_types: list[SourceType] | None = Field(
        default=None, description="限定检索源。null=不限"
    )
    chunk_types: list[ChunkType] | None = Field(default=None, description="限定切片粒度")
    contract_type: ContractType | None = Field(
        default=None, description="同类型优先过滤（软过滤，命中不足时自动放宽）"
    )
    law_codes: list[str] | None = Field(default=None, description="限定法规代码")
    doc_ids: list[str] | None = Field(default=None, description="限定源文档")
    categories: list[str] | None = Field(default=None, description="限定业务/风险类别")


class RAGQueryRequest(BaseModel):
    """检索请求契约。

    Attributes:
        trace_id: 全链路追踪 ID，调用方不传则由本模型自动生成。
        query: 检索文本（待审条款原文 / 用户问题 / 风险点描述）。
        top_k: 最终返回条数上限。
        score_threshold: 相关性下限，低于此值判为"无匹配标准基线"。
        strategy: 检索策略，Phase 1 固定 DENSE_ONLY。
        filters: 检索过滤条件。
        max_context_tokens: 注入 Prompt 的上下文 Token 预算。
        timeout_ms: 整体超时预算（内部按段分配）。
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(default_factory=lambda: str(uuid4()), description="全链路追踪 ID")
    query: str = Field(min_length=1, max_length=4000, description="检索文本")
    top_k: int = Field(default=8, ge=1, le=50, description="最终返回条数上限")
    score_threshold: float = Field(
        default=DEFAULT_SCORE_THRESHOLD, ge=0.0, le=1.0, description="相关性下限"
    )
    strategy: RetrieveStrategy = Field(
        default=RetrieveStrategy.HYBRID_RRF, description="检索策略"
    )
    enable_rerank: bool = Field(default=True, description="是否启用精排")
    rerank_top_n: int = Field(
        default=5, ge=1, le=20, description="重排后保留条数（3~5 为宜）"
    )
    enable_sparse: bool = Field(default=True, description="是否启用 BM25 路（Phase 2+）")
    filters: RetrievalFilters = Field(
        default_factory=RetrievalFilters, description="检索过滤条件"
    )
    max_context_tokens: int = Field(
        default=3000, ge=256, le=32000, description="上下文 Token 预算"
    )
    timeout_ms: int = Field(default=3000, ge=100, le=30000, description="整体超时（毫秒）")
    with_highlight: bool = Field(default=False, description="是否返回命中词高亮片段")


class TokenUsage(BaseModel):
    """Token 耗用统计。可观测性必打。

    `total_tokens` 由 `add()` 自动重算，禁止手工赋值。
    """

    model_config = ConfigDict(extra="forbid")

    embedding_tokens: int = Field(default=0, ge=0, description="向量化耗用")
    rerank_tokens: int = Field(default=0, ge=0, description="精排耗用")
    prompt_tokens: int = Field(default=0, ge=0, description="Prompt 耗用")
    completion_tokens: int = Field(default=0, ge=0, description="生成耗用")
    total_tokens: int = Field(default=0, ge=0, description="合计（自动累加）")
    estimated: bool = Field(default=True, description="true=估算值（无真实 tokenizer 时）")

    def add(self, **kwargs: int) -> None:
        """累加各分项并同步 total_tokens。

        Args:
            **kwargs: `embedding_tokens` / `rerank_tokens` / `prompt_tokens` /
                `completion_tokens` 中的任意组合。传入 `total_tokens` 会被忽略。
        """
        for key, value in kwargs.items():
            if key == "total_tokens" or not hasattr(self, key):
                continue
            setattr(self, key, getattr(self, key) + int(value))
        self.total_tokens = (
            self.embedding_tokens
            + self.rerank_tokens
            + self.prompt_tokens
            + self.completion_tokens
        )


class ChunkScores(BaseModel):
    """多阶段得分。保留全链路分数用于调试与阈值调参。"""

    model_config = ConfigDict(extra="forbid")

    dense: float | None = Field(default=None, description="向量余弦相似度 [-1,1]")
    sparse: float | None = Field(default=None, description="BM25 原始分（未归一）")
    fused: float | None = Field(default=None, description="RRF 融合分 Σ1/(k+rank)，k=60")
    rerank: float | None = Field(default=None, description="Cross-Encoder 精排分 [0,1]")
    final: float = Field(ge=0.0, le=1.0, description="最终用于阈值比较的分数")
    rank: int = Field(ge=1, description="最终排名（1-based）")


class CitationOut(BaseModel):
    """与 `auditor.PreciseCitation` 字段 1:1（camelCase，供前端直接消费）。

    ⚠️ 冻结字段：sourceType / sourceTitle / contentChunk / relevanceScore。
    任何变更必须先改设计文档 §2.0.1 并同步 auditor.py。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sourceType: SourceType = Field(description="检索源类型")
    sourceTitle: str = Field(description="来源标题（逐级兜底，保证非空）")
    contentChunk: str = Field(description="原文片段（超 800 字截断）")
    relevanceScore: float = Field(ge=0.0, le=1.0, description="相关性得分 [0,1]")


class RetrievedDoc(BaseModel):
    """单条召回结果。已排序、已过滤、已截断。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(description="切片 ID（与 Qdrant point_id 一致）")
    doc_id: str = Field(description="源文档 ID")
    source_type: SourceType = Field(description="检索源类型")
    chunk_type: ChunkType = Field(description="切片粒度")
    section_id: str = Field(description="章节/条款编号")
    law_code: str | None = Field(default=None, description="法规代码")
    article_no: str | None = Field(default=None, description="法条号")
    title: str | None = Field(default=None, description="条款/法条标题")
    text: str = Field(description="切片正文")
    highlight: str | None = Field(default=None, description="命中词高亮片段")
    page_no: int | None = Field(default=None, ge=1, description="物理页码")
    bbox: BBox | None = Field(default=None, description="物理坐标")
    contract_type: ContractType | None = Field(default=None, description="合同类型")
    category: str = Field(default="UNSPECIFIED", description="业务/风险类别")
    section_path: str = Field(default="", description="人类可读层级路径")
    token_count: int = Field(default=0, ge=0, description="该切片的 Token 数")
    scores: ChunkScores = Field(description="多阶段得分")
    citation: CitationOut = Field(description="可直接赋值给 EnhancedRiskItem.citations")


class RAGQueryResponse(BaseModel):
    """检索响应契约。

    `degraded=true` 时调用方（LLM）必须明确知道"没有依据"，否则会幻觉出法条。
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(description="全链路追踪 ID（与请求一致或服务端生成）")
    query: str = Field(description="回显请求 query（可能为截断版本）")
    strategy: RetrieveStrategy = Field(
        default=RetrieveStrategy.HYBRID_RRF, description="实际生效的检索策略"
    )
    retrieved_docs: list[RetrievedDoc] = Field(
        default_factory=list, description="最终召回文档"
    )
    total_candidates: int = Field(default=0, ge=0, description="融合前的候选总数")
    context_block: str = Field(default="", description="可直接注入 Prompt 的上下文文本")
    context_tokens: int = Field(default=0, ge=0, description="上下文实际 Token 数")
    truncated: bool = Field(default=False, description="是否因 Token 预算被截断")
    degraded: bool = Field(default=False, description="是否走了降级路径")
    fallback_reason: FallbackReason = Field(
        default=FallbackReason.NONE, description="降级原因"
    )
    warnings: list[str] = Field(default_factory=list, description="非致命告警列表")
    token_usage: TokenUsage = Field(
        default_factory=TokenUsage, description="Token 耗用统计"
    )
    stage_latency_ms: dict[str, int] = Field(
        default_factory=dict,
        description="分段耗时：embed/dense/sparse/fusion/rerank/build",
    )
    took_ms: int = Field(default=0, ge=0, description="整体耗时（毫秒）")
    schema_version: Literal["1.0.0"] = Field(default=SCHEMA_VERSION, description="契约版本")


__all__ = [
    "DEFAULT_RRF_K",
    "DEFAULT_SCORE_THRESHOLD",
    "SCHEMA_VERSION",
    "BBox",
    "ChunkScores",
    "ChunkType",
    "CitationOut",
    "ContractType",
    "FallbackReason",
    "RAGQueryRequest",
    "RAGQueryResponse",
    "RetrieveStrategy",
    "RetrievedDoc",
    "RetrievalFilters",
    "SourceType",
    "TokenUsage",
]
