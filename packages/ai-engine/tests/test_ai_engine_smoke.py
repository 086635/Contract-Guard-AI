"""ai-engine 冒烟自检（T-A0-4）。

本包此前**零测试**，而 `0.§0.9` DoD 要求每个任务「单测已加」；
在 T-B0 ~ T-B7 落地前，这两条断言是 ai-engine 唯一的回归锚点。
"""

from __future__ import annotations


def test_package_is_importable() -> None:
    """包本体可导入且已声明版本。"""
    import ai_engine

    assert ai_engine.__version__ == "1.1.0"


def test_workspace_dependencies_resolve() -> None:
    """T-A0-3：三个 workspace 兄弟包必须可导入（声明缺失时此处立刻红）。"""
    from cg_observability import TokenCounter
    from rag_contracts.chunk import EMBEDDING_DIM, ContractChunk
    from rag_core.service import RAGService

    assert EMBEDDING_DIM == 1024
    assert ContractChunk.__name__ == "ContractChunk"
    assert TokenCounter(force_estimate=True).count("甲乙双方应当履行") >= 1
    assert hasattr(RAGService, "retrieve")
