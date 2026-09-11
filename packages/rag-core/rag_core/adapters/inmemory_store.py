"""内存向量库适配器：实现 `rag_contracts.ports.VectorStorePort`。

用途：Phase 1 默认实现 / 单元测试 / 离线演示。
**零外部依赖**：纯 Python 余弦相似度，不引入 numpy，保证任何环境都能跑。

生产替换：改用 `QdrantVectorStore` 即可，业务代码零改动。
"""

from __future__ import annotations

import math
from bisect import insort
from typing import Sequence

from rag_contracts.ports import VectorHit, VectorRecord
from rag_contracts.query import RetrievalFilters


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """计算余弦相似度（纯 Python 实现）。

    Args:
        a: 向量 A。
        b: 向量 B。

    Returns:
        [-1, 1] 区间的相似度；零向量或长度不符时返回 0.0。
    """
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _matches(payload: dict, filters: RetrievalFilters | None) -> bool:
    """判断 payload 是否满足过滤条件（null 表示该维度不限）。

    Args:
        payload: 切片 payload（已 JSON 化）。
        filters: 过滤条件。

    Returns:
        True 表示通过过滤。
    """
    if filters is None:
        return True
    if filters.source_types is not None and payload.get("source_type") not in [
        s.value if hasattr(s, "value") else s for s in filters.source_types
    ]:
        return False
    if filters.chunk_types is not None and payload.get("chunk_type") not in [
        c.value if hasattr(c, "value") else c for c in filters.chunk_types
    ]:
        return False
    if filters.contract_type is not None:
        expected = filters.contract_type
        expected = expected.value if hasattr(expected, "value") else expected
        if payload.get("contract_type") != expected:
            return False
    if filters.law_codes is not None and payload.get("law_code") not in filters.law_codes:
        return False
    if filters.doc_ids is not None and payload.get("doc_id") not in filters.doc_ids:
        return False
    if filters.categories is not None and payload.get("category") not in filters.categories:
        return False
    return True


class InMemoryVectorStore:
    """进程内向量库：dict 存储 + 暴力余弦检索。

    时间复杂度 O(N·D)，N 为切片数、D 为维度；Phase 1 量级（千级）完全够用。
    """

    def __init__(self) -> None:
        """初始化空库。"""
        self._vectors: dict[str, list[float]] = {}
        self._payloads: dict[str, dict] = {}
        self._dim: int | None = None
        self.name: str = "inmemory"

    # ---------------- VectorStorePort ----------------
    def ensure_collection(self, dim: int) -> None:
        """登记维度（幂等）。

        Args:
            dim: 向量维度。
        """
        self._dim = int(dim)

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """幂等写入：`chunk_id` 相同则覆盖。

        Args:
            records: （chunk, vector）序列。

        Returns:
            成功写入条数（维度不符的记录会被跳过）。
        """
        written = 0
        for record in records:
            if self._dim is not None and len(record.vector) != self._dim:
                continue
            self._vectors[record.chunk.chunk_id] = list(record.vector)
            self._payloads[record.chunk.chunk_id] = record.chunk.to_payload()
            written += 1
        return written

    def search(
        self,
        vector: Sequence[float],
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[VectorHit]:
        """暴力余弦检索，按得分降序返回。

        Args:
            vector: 查询向量。
            top_k: 返回条数上限。
            filters: 过滤条件。

        Returns:
            VectorHit 列表（得分降序）。
        """
        scored: list[tuple[float, str]] = []
        for chunk_id, candidate in self._vectors.items():
            payload = self._payloads.get(chunk_id, {})
            if not _matches(payload, filters):
                continue
            score = cosine(vector, candidate)
            # 用 (-score, chunk_id) 排序：得分降序，同分按 chunk_id 稳定排序
            insort(scored, (-score, chunk_id))

        return [
            VectorHit(
                chunk_id=chunk_id,
                score=-neg_score,
                payload=dict(self._payloads.get(chunk_id, {})),
            )
            for neg_score, chunk_id in scored[: max(0, top_k)]
        ]

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除全部切片。

        Args:
            doc_id: 源文档标识。

        Returns:
            删除条数。
        """
        victims = [
            cid for cid, payload in self._payloads.items() if payload.get("doc_id") == doc_id
        ]
        for cid in victims:
            self._vectors.pop(cid, None)
            self._payloads.pop(cid, None)
        return len(victims)

    def health(self) -> bool:
        """健康检查：内存库永远可用。"""
        return True

    # ---------------- 调试辅助 ----------------
    def count(self) -> int:
        """当前库内向量条数。"""
        return len(self._vectors)

    def clear(self) -> None:
        """清空全部数据（单测用）。"""
        self._vectors.clear()
        self._payloads.clear()


__all__ = ["InMemoryVectorStore", "cosine"]
