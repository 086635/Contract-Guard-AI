"""端口（Protocol）：业务层与基础设施层的唯一边界（RAG_MODULE_DESIGN.md §2.4）。

所有 Adapter（Qdrant / BM25 / Embedding / Reranker）必须实现对应 Protocol；
业务层（RAGService）只依赖 Protocol，不 import 任何 SDK。
→ Phase 1/2/3 替换存储不侵入业务代码，只改 DI 工厂。

`@runtime_checkable` 使得 `isinstance(adapter, VectorStorePort)` 可用，
便于在工厂与单测中做结构性断言（仅校验方法名存在，不校验签名）。
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .chunk import ContractChunk
from .query import RetrievalFilters


class VectorRecord(BaseModel):
    """写入向量库的记录：chunk + 向量。"""

    model_config = ConfigDict(extra="forbid")

    chunk: ContractChunk = Field(description="切片契约对象")
    vector: list[float] = Field(min_length=1, description="稠密向量（项目锁定 1024 维）")


class VectorHit(BaseModel):
    """向量库返回的单条命中。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(description="切片 ID")
    score: float = Field(description="相似度得分（COSINE，越大越相似）")
    payload: dict = Field(default_factory=dict, description="完整 ContractChunk payload")


class SparseHit(BaseModel):
    """BM25 返回的单条命中。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(description="切片 ID")
    score: float = Field(description="BM25 原始分（未归一，越大越相似）")
    text: str = Field(default="", description="命中文本（调试用）")


@runtime_checkable
class VectorStorePort(Protocol):
    """向量库端口。实现：QdrantVectorStore / InMemoryVectorStore。"""

    def ensure_collection(self, dim: int) -> None:
        """建集合 + payload keyword 索引。幂等。

        Args:
            dim: 向量维度（项目锁定 1024）。
        """
        ...

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """幂等写入，point_id == chunk_id。

        Args:
            records: 待写入的（chunk, vector）序列。

        Returns:
            成功写入的条数。
        """
        ...

    def search(
        self,
        vector: Sequence[float],
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[VectorHit]:
        """稠密检索。

        Args:
            vector: 查询向量。
            top_k: 返回条数上限。
            filters: 过滤条件，None 表示不过滤。

        Returns:
            按得分降序的命中列表。
        """
        ...

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除，供增量重建使用。

        Args:
            doc_id: 源文档标识。

        Returns:
            删除条数。
        """
        ...

    def health(self) -> bool:
        """健康检查，供降级判断。

        Returns:
            True 表示可用。
        """
        ...


@runtime_checkable
class SparseStorePort(Protocol):
    """稀疏检索（BM25）端口。Phase 2 实现。"""

    def upsert(self, chunks: Sequence[ContractChunk]) -> int:
        """写入倒排索引。

        Args:
            chunks: 切片序列。

        Returns:
            写入条数。
        """
        ...

    def search(self, query: str, top_k: int) -> list[SparseHit]:
        """BM25 检索。

        Args:
            query: 查询文本。
            top_k: 返回条数上限。

        Returns:
            按 BM25 分降序的命中列表。
        """
        ...

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除。

        Args:
            doc_id: 源文档标识。

        Returns:
            删除条数。
        """
        ...

    def health(self) -> bool:
        """健康检查。

        Returns:
            True 表示可用。
        """
        ...


@runtime_checkable
class EmbedderPort(Protocol):
    """向量化端口。实现：HttpEmbedder（TEI/OpenAI 兼容）/ HashEmbedder（离线兜底）。"""

    @property
    def model_name(self) -> str:
        """模型名（写入 chunk.embedding_model，换模型时可据此重建索引）。"""
        ...

    @property
    def dim(self) -> int:
        """向量维度。"""
        ...

    def embed_one(self, text: str) -> list[float]:
        """单条向量化。

        Args:
            text: 待向量化文本。

        Returns:
            长度为 dim 的浮点向量。
        """
        ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化。

        Args:
            texts: 文本序列。

        Returns:
            与 texts 等长的向量列表。
        """
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """重排端口。实现：NoopReranker / CrossEncoderReranker / HttpReranker。"""

    @property
    def model_name(self) -> str:
        """重排模型名（"noop" 表示未启用）。"""
        ...

    def rerank(
        self,
        query: str,
        texts: Sequence[str],
        base_scores: Sequence[float] | None = None,
    ) -> list[float]:
        """精排打分。

        签名统一为三参（C1 决策）：`base_scores` 为上游得分（稠密余弦或 RRF 融合分），
        实现可在重排不可用时原样透传该值作为降级。所有实现（NoopReranker /
        HttpReranker）均已支持第三参，故本协议不再需要 `try/except TypeError` 兜底。

        Args:
            query: 查询文本。
            texts: 候选文本序列。
            base_scores: 上游得分序列，长度与 texts 一致；可为 None。

        Returns:
            与 texts 等长的相关性分，建议归一化到 [0,1]。
        """
        ...


@runtime_checkable
class TokenCounterPort(Protocol):
    """Token 计数端口。实现：cg_observability.TokenCounter。"""

    def count(self, text: str) -> int:
        """统计单条文本的 Token 数。

        Args:
            text: 输入文本。

        Returns:
            Token 数。
        """
        ...

    def count_many(self, texts: Sequence[str]) -> int:
        """统计多条文本的 Token 总数。

        Args:
            texts: 文本序列。

        Returns:
            Token 总数。
        """
        ...


__all__ = [
    "EmbedderPort",
    "RerankerPort",
    "SparseHit",
    "SparseStorePort",
    "TokenCounterPort",
    "VectorHit",
    "VectorRecord",
    "VectorStorePort",
]
