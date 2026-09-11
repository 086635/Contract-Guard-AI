"""依赖注入工厂：Phase 切换的**唯一改点**。

业务代码（RAGService）只依赖 Protocol，因此从"离线内存库 + 哈希向量"
切到"Qdrant + TEI + HTTP Rerank"只需改环境变量，本文件与业务代码都不用动。

示例：
    # 离线（默认）
    service = build_service_from_settings()

    # 生产
    CG_RAG_VECTOR_STORE=qdrant CG_RAG_EMBED_MODE=http ... python scripts/phase1_smoke.py
"""

from __future__ import annotations

from typing import Any

from cg_observability import TokenCounter, get_logger
from rag_contracts.ports import (
    EmbedderPort,
    RerankerPort,
    SparseStorePort,
    VectorStorePort,
)

from .adapters import HashEmbedder, HttpEmbedder, InMemoryVectorStore, NoopReranker
from .config import RagSettings, get_settings
from .service import RAGService

_LOG = get_logger("rag_core.factory")


def build_embedder(settings: RagSettings) -> EmbedderPort:
    """按配置构造向量化器。

    Args:
        settings: RAG 配置。

    Returns:
        EmbedderPort 实现。

    Raises:
        ValueError: `embed_mode` 取值非法。
    """
    if settings.embed_mode == "hash":
        return HashEmbedder(dim=settings.vector_dim, model_name="hash-trick-v1")
    if settings.embed_mode == "http":
        return HttpEmbedder(
            base_url=settings.embed_base_url,
            model=settings.embed_model,
            api_key=settings.embed_api_key,
            dim=settings.vector_dim,
            batch_size=settings.embed_batch_size,
            timeout_ms=settings.embed_timeout_ms,
            path=settings.embed_path,
        )
    raise ValueError(f"不支持的 embed_mode：{settings.embed_mode}")


def build_vector_store(settings: RagSettings) -> VectorStorePort:
    """按配置构造向量库。

    Args:
        settings: RAG 配置。

    Returns:
        VectorStorePort 实现。

    Raises:
        ValueError: `vector_store` 取值非法。
    """
    if settings.vector_store == "inmemory":
        return InMemoryVectorStore()
    if settings.vector_store == "qdrant":
        from .adapters import QdrantVectorStore  # 延迟导入：默认路径不加载 SDK

        return QdrantVectorStore(
            mode=settings.qdrant_mode,
            url=settings.qdrant_url,
            path=settings.qdrant_local_path,
            api_key=settings.qdrant_api_key,
            collection=settings.collection,
            timeout_s=settings.timeout_ms / 1000.0,
        )
    raise ValueError(f"不支持的 vector_store：{settings.vector_store}")


def build_reranker(settings: RagSettings) -> RerankerPort | None:
    """按配置构造重排器。

    Args:
        settings: RAG 配置。

    Returns:
        RerankerPort 实现；`rerank_enabled=False` 时返回 None。
    """
    if not settings.rerank_enabled:
        return None
    if settings.rerank_mode == "noop":
        return NoopReranker()
    if settings.rerank_mode in ("local", "http"):
        # Phase 2/3 落地 CrossEncoderReranker / HttpReranker；
        # 当前先降级为 noop，保证配置项提前可用且不影响链路。
        _LOG.warning("rerank_mode=%s 尚未实现，已回退 noop", settings.rerank_mode)
        return NoopReranker()
    return NoopReranker()


def build_sparse_store(settings: RagSettings) -> SparseStorePort | None:
    """按配置构造 BM25 稀疏检索器。

    Args:
        settings: RAG 配置。

    Returns:
        Phase 1/2 暂无默认实现，恒返回 None（T03 接入 BM25Store）。
    """
    del settings  # 预留：T03 根据 bm25_dir 构造 BM25Store
    return None


def build_service_from_settings(
    settings: RagSettings | None = None, **overrides: Any
) -> RAGService:
    """从配置构造完整的 RAGService。

    Args:
        settings: 配置；为空则用 `get_settings()` 单例。
        **overrides: 临时覆盖的构造参数（如 `dense_top`），便于单测。

    Returns:
        可直接调用 `retrieve()` 的 RAGService。
    """
    cfg = settings or get_settings()
    service = RAGService(
        embedder=build_embedder(cfg),
        vector_store=build_vector_store(cfg),
        counter=TokenCounter(),
        reranker=build_reranker(cfg),
        sparse_store=build_sparse_store(cfg),
        dense_top=cfg.dense_top,
        default_max_context_tokens=cfg.max_context_tokens,
    )
    for key, value in overrides.items():
        if hasattr(service, f"_{key}"):
            setattr(service, f"_{key}", value)
    return service


__all__ = [
    "build_embedder",
    "build_reranker",
    "build_service_from_settings",
    "build_sparse_store",
    "build_vector_store",
]
