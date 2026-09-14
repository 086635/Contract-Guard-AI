"""RRF 融合自检（设计文档 T03-3 / §8.6 / B.2.2）。"""

from __future__ import annotations

import pytest

from rag_contracts.query import DEFAULT_RRF_K
from rag_core.fusion import rrf_fuse


def test_fuse_formula_both_lists_hit() -> None:
    """两路均命中 b：b 的 fused = 1/(k+2) + 1/(k+1)，误差 < 1e-9。"""
    k = 60
    dense = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
    sparse = [("b", 3.0), ("a", 2.0), ("c", 1.0)]
    fused = dict(rrf_fuse([dense, sparse], k=k))
    # b 在 dense 排第 2、sparse 排第 1 → 1/(k+2) + 1/(k+1)
    expected = 1 / (k + 2) + 1 / (k + 1)
    assert fused["b"] == pytest.approx(expected, abs=1e-9)


def test_single_list_missing_still_present() -> None:
    """只在一路出现的 d 仍出现在结果里。"""
    dense = [("a", 0.9), ("d", 0.5)]
    sparse = [("a", 3.0)]
    result = dict(rrf_fuse([dense, sparse]))
    assert "d" in result
    # d 只在 dense 排第 2 → 1/(k+2)
    assert result["d"] == pytest.approx(1 / (60 + 2), abs=1e-9)


def test_tied_first_in_both_lists() -> None:
    """两路并列第一时，fused = 2/(k+1)。"""
    dense = [("a", 0.9), ("b", 0.8)]
    sparse = [("a", 3.0), ("b", 2.0)]
    fused = dict(rrf_fuse([dense, sparse]))
    assert fused["a"] == pytest.approx(2 / 61, abs=1e-9)


def test_empty_inputs() -> None:
    """空输入返回 []。"""
    assert rrf_fuse([]) == []
    assert rrf_fuse([[]]) == []


def test_sorted_desc() -> None:
    """结果按 fused 严格降序。"""
    dense = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
    sparse = [("c", 3.0), ("b", 2.0), ("a", 1.0)]
    result = rrf_fuse([dense, sparse])
    scores = [score for _, score in result]
    assert scores == sorted(scores, reverse=True)


def test_k_smoothing_semantics() -> None:
    """k 变大后，第 1 名与第 10 名的分差变小。"""

    def gap(k: int) -> float:
        ranked = [(f"d{i}", 1.0 - i) for i in range(10)]
        result = rrf_fuse([ranked], k=k)
        return result[0][1] - result[-1][1]

    assert gap(100) < gap(10)


def test_default_k_is_drrf_k() -> None:
    """默认 k 取 DEFAULT_RRF_K（60）。"""
    ranked = [("a", 1.0)]
    result = rrf_fuse([ranked])
    assert result == [("a", pytest.approx(1 / (DEFAULT_RRF_K + 1), abs=1e-9))]
