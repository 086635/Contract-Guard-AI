"""RAG 重排（Reranker）精确打分。

对混合检索召回的候选做 Cross-Encoder 重排，输出 relevanceScore（§8.4 的
[0,1] 向量匹配度），用于 PreciseCitation.relevanceScore。
"""

from __future__ import annotations

from .retriever import RetrievedChunk


class Reranker:
    """重排打分骨架。

    TODO（工程实现）：接入 Cross-Encoder / 重排模型，对 chunks 与 query 做
    精细相关性打分，裁剪 top_n 供 LLM 引用。
    """

    def __init__(self, top_n: int = 4) -> None:
        self.top_n = top_n

    def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        raise NotImplementedError("reranker.rerank 待实现：接入 Cross-Encoder 重排")
