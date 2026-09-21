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
    """按配置构造重排器（T03-5 接入 HTTP 精排）。

    Args:
        settings: RAG 配置。

    Returns:
        RerankerPort 实现；`rerank_enabled=False` 时返回 None。
    """
    if not settings.rerank_enabled:
        return None
    if settings.rerank_mode == "noop":
        return NoopReranker()
    if settings.rerank_mode == "http":
        if not settings.rerank_http_url:
            # 配置缺失属"降级"而非"崩溃"：回退 noop 并留 warning 供排查
            _LOG.warning("rerank_mode=http 但未配置 rerank_http_url，已回退 noop")
            return NoopReranker()
        from .adapters import HttpReranker

        return HttpReranker(
            url=settings.rerank_http_url,
            model=settings.rerank_model,
            api_key=settings.rerank_api_key,
            timeout_ms=settings.rerank_timeout_ms,
            query_path=settings.rerank_path,
        )
    if settings.rerank_mode == "local":
        # 硬约束：rag-core 禁止引入 torch / sentence-transformers
        _LOG.warning("rerank_mode=local 需 torch，本项目禁用，已回退 noop")
        return NoopReranker()
    return NoopReranker()


def build_sparse_store(settings: RagSettings) -> SparseStorePort | None:
    """按配置构造 BM25 稀疏检索器（T03-1 接入）。

    Args:
        settings: RAG 配置。

    Returns:
        BM25Store 实例；`bm25_enabled=False` 时返回 None（策略会自动退化为 DENSE_ONLY）。
    """
    if not settings.bm25_enabled:
        return None
    # 函数内延迟导入（§B.2.6）：统一走适配器包出口，与顶部 import 风格保持一致
    from .adapters import BM25Store

    return BM25Store(data_dir=settings.bm25_dir)


def _rerank_threshold_for(settings: RagSettings) -> float | None:
    """精排阈值是否下发（T-A7 的「分工」实现）。

    只有**精排真正生效**时才下发 `rerank_threshold`，否则返回 None：
    `noop` / `local`（本项目禁用，工厂会回退 noop）/ `http` 但未配 URL（同样回退 noop）
    这三种情况下的「精排分」其实就是上游分，再叠加一个 0.35 的阈值等于把
    `score_threshold` 重复扣两次，会把离线默认路径（低阈值 + NoopReranker）打成空召回。

    Args:
        settings: RAG 配置。

    Returns:
        精排分下限；精排不生效时为 None。
    """
    if not settings.rerank_enabled or settings.rerank_mode in ("noop", "local"):
        return None
    if settings.rerank_mode == "http" and not settings.rerank_http_url:
        return None  # build_reranker 会回退 noop，见上
    return settings.rerank_threshold


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
        sparse_top=cfg.sparse_top,
        rrf_k=cfg.rrf_k,
        default_max_context_tokens=cfg.max_context_tokens,
        # T-A7：把 4 个「声明了但无消费者」的配置项接到服务级兜底上
        default_strategy=cfg.strategy,
        default_score_threshold=cfg.score_threshold,
        rerank_top_n=cfg.rerank_top_n,
        rerank_threshold=_rerank_threshold_for(cfg),
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
