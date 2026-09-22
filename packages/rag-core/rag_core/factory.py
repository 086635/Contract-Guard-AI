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
from .provider_presets import detect_provider, resolve_preset
from .service import RAGService

_LOG = get_logger("rag_core.factory")


def _looks_like_url(value: str) -> bool:
    """判断配置值是 URL 还是裸 provider 名。

    Args:
        value: `CG_RAG_EMBED_BASE_URL` 的取值。

    Returns:
        True 表示是以 http(s):// 开头的 URL。
    """
    return value.strip().lower().startswith(("http://", "https://"))


def _resolve_embed_endpoint(settings: RagSettings) -> dict[str, str]:
    """用 provider 预设补全**留空**的 embedding 端点（`1.C3` §⑤/§⑥）。

    provider 只能从 `embed_base_url` 反查（本卡不新增配置项，反查规则见
    `provider_presets.detect_provider`）：

    - 值写成 provider 名（`ollama` / `tei` / `openai`）→ 用预设的 `base_url` + `path` + `model`；
    - 值是 URL 且 `host:port` 命中预设 → **只补** `path` / `model`（显式配了就不补）；
    - 其它（用户自定义端点）→ 全部按配置值走，**不套预设**。

    Args:
        settings: RAG 配置。

    Returns:
        含 `base_url` / `path` / `model` / `provider` 的字典。
    """
    provider = detect_provider("embed", settings.embed_base_url)
    preset = resolve_preset("embed", provider) if provider else {}
    explicit = settings.model_fields_set
    base_url = settings.embed_base_url
    if provider and not _looks_like_url(base_url):
        # 「留空」的一种形式：值写成 provider 名（如 CG_RAG_EMBED_BASE_URL=ollama）
        base_url = preset["base_url"]
    path = (
        settings.embed_path
        if "embed_path" in explicit
        else preset.get("path", settings.embed_path)
    )
    model = (
        settings.embed_model
        if "embed_model" in explicit
        else preset.get("model", settings.embed_model)
    )
    return {
        "base_url": base_url,
        "path": path,
        "model": model,
        "provider": provider or "",
    }


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
        endpoint = _resolve_embed_endpoint(settings)
        return HttpEmbedder(
            base_url=endpoint["base_url"],
            model=endpoint["model"],
            api_key=settings.embed_api_key,
            dim=settings.vector_dim,
            batch_size=settings.embed_batch_size,
            timeout_ms=settings.embed_timeout_ms,
            path=endpoint["path"],
            provider=endpoint["provider"],
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


def _resolve_rerank_endpoint(settings: RagSettings) -> dict[str, str]:
    """用 provider 预设补全**留空**的 rerank 端点（`1.C4` §⑤/§⑥）。

    与 `_resolve_embed_endpoint` 同款规则（provider 反查 → 只补留空）：

    - `rerank_http_url` 写成 provider 名（`tei`）→ 用预设的 `base_url` + `path` + `model`；
    - 值是 URL 且 `host:port` 命中预设 → **只补** `path` / `model`（显式配了就不补）；
    - 其它（用户自建端点，例如本机 rerank 服务）→ **不套预设**，全按配置值走。

    ⚠️ 与 embedding 的差异：`rerank_http_url` **留空**时**不套预设**，而是沿用既有降级
    （warning + `NoopReranker`，见 `1.C4` §⑤ 行为表）—— 故本函数的入参一定是非空 URL。

    Args:
        settings: RAG 配置。

    Returns:
        含 `url` / `path` / `model` / `provider` 的字典。
    """
    provider = detect_provider("rerank", settings.rerank_http_url)
    preset = resolve_preset("rerank", provider) if provider else {}
    explicit = settings.model_fields_set
    url = settings.rerank_http_url
    if provider and not _looks_like_url(url):
        # 「留空」的一种形式：值写成 provider 名（如 CG_RAG_RERANK_HTTP_URL=tei）
        url = preset["base_url"]
    path = (
        settings.rerank_path
        if "rerank_path" in explicit
        else preset.get("path", settings.rerank_path)
    )
    model = (
        settings.rerank_model
        if "rerank_model" in explicit
        else preset.get("model", settings.rerank_model)
    )
    return {"url": url, "path": path, "model": model, "provider": provider or ""}


def build_reranker(settings: RagSettings) -> RerankerPort | None:
    """按配置构造重排器（T03-5 接入 HTTP 精排；T-A6 接 provider 预设）。

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

        endpoint = _resolve_rerank_endpoint(settings)
        if endpoint["provider"] == "ollama":
            # 实测（2026-09-22）：ollama 0.34 **没有** rerank 端点（/rerank、/v1/rerank、
            # /api/rerank 均 404）→ 这么配会 RERANK_FAILED 静默回退。预设条目为兼容
            # `1.C4 §⑤` 与 `.env.example` 而保留，但必须在装配处显式告警（`1-6 §⑩` 避坑 1/4）。
            _LOG.warning(
                "rerank 命中 ollama 预设，但 ollama 不提供 rerank 端点（实测 404）——"
                "本地精排请改用 TEI（CG_RAG_RERANK_HTTP_URL=http://localhost:8081）"
            )
        return HttpReranker(
            url=endpoint["url"],
            model=endpoint["model"],
            api_key=settings.rerank_api_key,
            timeout_ms=settings.rerank_timeout_ms,
            query_path=endpoint["path"],
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
