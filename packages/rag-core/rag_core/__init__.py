"""rag-core：RAG 检索核心实现。

对外只需三件事：
1. `RAGService.retrieve(request) -> response` —— 在线检索
2. `SimpleChunker.chunk(doc) -> list[ContractChunk]` —— 离线切片
3. `build_service_from_settings()` —— 依赖注入工厂（Phase 切换唯一改点）

依赖方向（设计文档 §3.3 分层红线）：rag-core → rag-contracts / cg-observability，
**禁止**反向依赖与依赖 `apps/*`。
"""

from __future__ import annotations

from .config import RagSettings, get_settings
from .context_builder import ContextBuilder, ContextBuildResult
from .factory import (
    build_embedder,
    build_reranker,
    build_service_from_settings,
    build_sparse_store,
    build_vector_store,
)
from .service import RAGService

__version__ = "1.0.0"

__all__ = [
    "ContextBuilder",
    "ContextBuildResult",
    "RAGService",
    "RagSettings",
    "__version__",
    "build_embedder",
    "build_reranker",
    "build_service_from_settings",
    "build_sparse_store",
    "build_vector_store",
    "get_settings",
]
