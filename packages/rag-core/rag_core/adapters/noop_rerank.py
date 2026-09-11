"""重排适配器（Phase 1）：`NoopReranker`。

设计要点：
- `noop` 是**恒等透传**（identity），不是"全部打 0 分"。
  语义上表示"本阶段未启用精排"，因此 `final` 分直接沿用上游得分，
  保证 Phase 1 的阈值过滤与排序行为与 Phase 2（接真实模型）保持一致。
- Phase 2/3 在本文件追加 `CrossEncoderReranker` / `HttpReranker`，
  `RerankerPort` 不变 → 业务代码零改动。
"""

from __future__ import annotations

from typing import Sequence

from rag_contracts.citation import clamp_score  # 与契约层共用同一 clamp 口径

MODEL_NAME: str = "noop"


class NoopReranker:
    """恒等重排器：把上游得分（dense / fused）clamp 到 [0,1] 后原样返回。

    用法：
        reranker.rerank(query, texts, base_scores=[0.9, 0.7])
    """

    @property
    def model_name(self) -> str:
        """模型标识（"noop" 表示未启用精排）。"""
        return MODEL_NAME

    def rerank(
        self,
        query: str,
        texts: Sequence[str],
        base_scores: Sequence[float] | None = None,
    ) -> list[float]:
        """返回与 texts 等长的"精排分"。

        ⚠️ 本实现不做任何语义计算：有 base_scores 则透传，无则全 0。
        真实实现会把 (query, text) 成对送入 Cross-Encoder，
        两者对 `RerankerPort` 的调用方式完全一致。

        Args:
            query: 查询文本（noop 不使用）。
            texts: 候选文本（用于确定返回长度）。
            base_scores: 上游得分；为空则退化为全 0（此时必然触发 EMPTY_RECALL 降级）。

        Returns:
            与 texts 等长、已 clamp 到 [0,1] 的分数列表。
        """
        del query  # noop 不做语义计算
        n = len(texts)
        if base_scores is None:
            return [0.0] * n
        scores = [clamp_score(s) for s in list(base_scores)[:n]]
        scores.extend([0.0] * (n - len(scores)))
        return scores


__all__ = ["MODEL_NAME", "NoopReranker"]
