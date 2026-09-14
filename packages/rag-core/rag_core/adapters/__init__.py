"""适配器统一出口。

上层（factory / service）只从本包导入，具体实现的替换不影响调用方。
各实现均为**延迟导入重型 SDK**：默认路径（inmemory + hash）不会触碰 qdrant / httpx。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .bm25_store import BM25Store
from .embedding import HashEmbedder, HttpEmbedder, tokenize
from .inmemory_store import InMemoryVectorStore, cosine
from .noop_rerank import NoopReranker

if TYPE_CHECKING:
    # 仅供类型检查器解析 __getattr__ 懒导出的属性类型（PEP 562）：
    # 缺此块时 mypy 会把 QdrantVectorStore / HttpReranker 推断为 object，
    # 在调用处报 `"object" not callable`。运行时不执行此块，仍由 __getattr__ 延迟导入。
    # 注：BM25Store 已在运行时急切导入，类型检查器可直接解析，无需重复登记。
    from .http_reranker import HttpReranker
    from .qdrant_store import QdrantVectorStore

__all__ = [
    "BM25Store",
    "HashEmbedder",
    "HttpEmbedder",
    "HttpReranker",
    "InMemoryVectorStore",
    "NoopReranker",
    "QdrantVectorStore",
    "cosine",
    "tokenize",
]


def __getattr__(name: str) -> object:
    """延迟导出 `QdrantVectorStore` / `HttpReranker`（避免默认路径 import 重型 SDK）。

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
    if name == "HttpReranker":
        from .http_reranker import HttpReranker

        return HttpReranker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
