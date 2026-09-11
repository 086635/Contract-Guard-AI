"""适配器统一出口。

上层（factory / service）只从本包导入，具体实现的替换不影响调用方。
各实现均为**延迟导入重型 SDK**：默认路径（inmemory + hash）不会触碰 qdrant / httpx。
"""

from __future__ import annotations

from .embedding import HashEmbedder, HttpEmbedder, tokenize
from .inmemory_store import InMemoryVectorStore, cosine
from .noop_rerank import NoopReranker

__all__ = [
    "HashEmbedder",
    "HttpEmbedder",
    "InMemoryVectorStore",
    "NoopReranker",
    "QdrantVectorStore",
    "cosine",
    "tokenize",
]


def __getattr__(name: str) -> object:
    """延迟导出 `QdrantVectorStore`（避免默认路径 import qdrant-client）。

    Args:
        name: 属性名。

    Returns:
        对应的类。

    Raises:
        AttributeError: 非延迟属性。
    """
    if name == "QdrantVectorStore":
        from .qdrant_store import QdrantVectorStore

        return QdrantVectorStore
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
