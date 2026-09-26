"""RAG 薄桥接层。

检索与重排实现统一位于 `rag_core`；本包只保留旧 worker 的弃用导入路径。
新代码必须使用 `ai_engine.agents.tools.RagTool`。
"""

from __future__ import annotations

import warnings
from typing import Any

from ai_engine.agents.tools import RagTool, build_rag_tool


class _DeprecatedRagSkeleton:
    """旧骨架的弃用占位类。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            "HybridRetriever / Reranker 已废弃，请改用 build_rag_tool()",
            DeprecationWarning,
            stacklevel=2,
        )
        raise NotImplementedError(
            "旧 ai_engine.rag 检索骨架已移除，请改用 build_rag_tool()"
        )


HybridRetriever = _DeprecatedRagSkeleton
Reranker = _DeprecatedRagSkeleton

__all__ = ["RagTool", "build_rag_tool"]
