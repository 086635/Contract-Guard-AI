"""RAG 混合检索（Dense + Sparse）。

对应 data_scheme.md §8.4：为风险审核 / 重写提供标准条款与法条的精确溯源。
混合检索 = 稠密向量（语义）+ 稀疏（BM25 / 关键词）召回后融合。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RetrievedChunk:
    id: str
    title: str
    text: str
    sourceType: str  # STANDARD_CLAUSE | LAW_ARTICLE | PRECEDENT
    score: float


class HybridRetriever:
    """混合检索器骨架。

    TODO（工程实现）：接入向量库（Qdrant）做 Dense 召回，接入 BM25 / ES 做
    Sparse 召回，最后以 RRF（Reciprocal Rank Fusion）融合排序。
    """

    def __init__(self, top_k: int = 8) -> None:
        self.top_k = top_k

    def retrieve(self, query: str, contract_type: str | None = None) -> list[RetrievedChunk]:
        raise NotImplementedError("retriever.retrieve 待实现：接入 Qdrant + BM25 融合")
