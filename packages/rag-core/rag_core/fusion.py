"""Reciprocal Rank Fusion（RRF）融合。

公式：score(d) = Σ_各路 1 / (k + rank_路(d))，**原始分数被完全丢弃**。
对应：RAG搭建框架.md §6.3 / §8.6。

为什么丢弃分数：dense 是余弦 [-1,1]，BM25 是 [0,+∞)，量纲不可比；
名次是天然的"可比值"，故只保留 rank。详见 RAG对照解析.md §4.1。
"""

from __future__ import annotations

from collections.abc import Sequence

from rag_contracts.query import DEFAULT_RRF_K


def rrf_fuse(
    ranked_lists: Sequence[Sequence[tuple[str, float]]],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float]]:
    """把多路有序结果融合为单一有序列表。

    Args:
        ranked_lists: 每路是 [(chunk_id, 原始分), ...]，**必须已按分数降序**。
        k: 平滑常数，默认 60（`DEFAULT_RRF_K`）。越大则名次差距越被抹平。

    Returns:
        按 RRF 分降序的 [(chunk_id, fused_score), ...]。fused 为 (0,1) 内的小数，**非归一**。
    """
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        # rank 从 1 开始（第一名是 1 不是 0）
        for rank, (chunk_id, _original_score) in enumerate(ranked, start=1):
            # _original_score 故意不使用 —— 这是 RRF 的核心
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: item[1], reverse=True)


__all__ = ["rrf_fuse"]
