"""RAG 模块配置：`CG_RAG_*` 环境变量（设计文档 §3.6.3）。

设计要点：
1. 统一 `env_prefix="CG_RAG_"`，与项目既有 `CG_` 约定一致（见 api-gateway/config.py）。
2. **默认值即可离线跑通**：`vector_store=inmemory` + `embed_mode=hash` + `rerank_mode=noop`，
   不需要 API Key、不需要外网、不需要 GPU。
3. 切换到生产只需改环境变量，业务代码零改动（DI 工厂见 `rag_core.factory`）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_contracts.enums import RetrieveStrategy


class RagSettings(BaseSettings):
    """RAG 模块全部可调参数。

    环境变量形如 `CG_RAG_QDRANT_MODE` / `CG_RAG_EMBED_MODEL`（前缀自动去除字段名前缀后大写）。
    """

    # 允许仓库中有其他 CG_ 前缀变量而不报错
    model_config = SettingsConfigDict(
        env_prefix="CG_RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 向量库
    # ------------------------------------------------------------------
    vector_store: Literal["inmemory", "qdrant"] = Field(
        default="inmemory",
        description="向量库实现。inmemory=纯 Python 零依赖（默认/单测）；qdrant=真实向量库",
    )
    qdrant_mode: Literal["server", "local_memory", "local_path"] = Field(
        default="local_memory",
        description="Qdrant 运行模式：server=连服务 / local_memory=内存 / local_path=本地磁盘",
    )
    qdrant_url: str = Field(default="http://localhost:6333", description="Qdrant 服务地址")
    qdrant_local_path: str = Field(default="./data/qdrant", description="本地磁盘模式的数据目录")
    qdrant_api_key: str | None = Field(default=None, description="Qdrant Cloud API Key")
    collection: str = Field(default="contract_clauses", description="集合名")
    vector_dim: int = Field(default=1024, ge=1, description="向量维度（bge-m3 = 1024）")

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    embed_mode: Literal["http", "hash"] = Field(
        default="hash",
        description="hash=离线确定性伪向量（默认，零依赖）；http=TEI / OpenAI 兼容 API",
    )
    embed_base_url: str = Field(default="http://localhost:8080", description="Embedding 服务地址")
    embed_api_key: str = Field(default="none", description="Embedding API Key")
    embed_model: str = Field(default="BAAI/bge-m3", description="Embedding 模型名")
    embed_batch_size: int = Field(default=32, ge=1, description="批量向量化大小")
    embed_timeout_ms: int = Field(default=3000, ge=100, description="单次请求超时（毫秒）")
    embed_path: str = Field(
        default="/v1/embeddings",
        description="接口路径。TEI 用 /embed，OpenAI 兼容用 /v1/embeddings",
    )

    # ------------------------------------------------------------------
    # 检索与融合（Phase 2 起生效）
    # ------------------------------------------------------------------
    bm25_enabled: bool = Field(
        default=True,
        description="是否启用 BM25 稀疏路。False 时 build_sparse_store 返回 None",
    )
    bm25_dir: str = Field(default="./data/bm25", description="BM25 索引持久化目录")
    sparse_top: int = Field(default=20, ge=1, description="稀疏路召回条数")
    dense_top: int = Field(default=20, ge=1, description="稠密路召回条数")
    rrf_k: int = Field(default=60, ge=1, description="RRF 融合常数（沿用前作 k=60）")

    # ------------------------------------------------------------------
    # Reranker
    # ------------------------------------------------------------------
    rerank_enabled: bool = Field(default=True, description="是否启用精排")
    rerank_mode: Literal["local", "http", "noop"] = Field(
        default="noop",
        description="noop=恒等透传（默认，Phase 1）；local=本地 CrossEncoder；http=远程服务",
    )
    rerank_model: str = Field(default="BAAI/bge-reranker-v2-m3", description="重排模型名")
    rerank_http_url: str = Field(default="", description="HTTP 重排服务地址")
    rerank_api_key: str = Field(default="none", description="HTTP 重排服务 Bearer Token")
    rerank_path: str = Field(default="/rerank", description="HTTP 重排接口路径")
    rerank_timeout_ms: int = Field(default=3000, ge=100, description="重排单次超时（毫秒）")
    rerank_threshold: float = Field(default=0.35, ge=0.0, le=1.0, description="重排后阈值")
    rerank_top_n: int = Field(default=5, ge=1, le=20, description="重排后保留条数")

    # ------------------------------------------------------------------
    # 预算、降级与默认值
    # ------------------------------------------------------------------
    max_context_tokens: int = Field(default=3000, ge=256, le=32000, description="上下文 Token 预算")
    max_query_chars: int = Field(default=2000, ge=100, description="工具入参 query 字符上限")
    timeout_ms: int = Field(default=3000, ge=100, le=30000, description="整体超时（毫秒）")
    max_retries: int = Field(default=3, ge=1, description="最大重试次数")
    score_threshold: float = Field(
        default=0.35, ge=0.0, le=1.0, description="相关性下限（沿用前作 0.35）"
    )
    strategy: RetrieveStrategy = Field(
        default=RetrieveStrategy.DENSE_ONLY,
        description="默认检索策略。Phase 1 固定 DENSE_ONLY；Phase 2 起改 HYBRID_RRF",
    )

    # ------------------------------------------------------------------
    # 缓存与日志
    # ------------------------------------------------------------------
    cache_ttl_s: int = Field(default=300, ge=0, description="热 Query 缓存 TTL（秒）")
    log_level: str = Field(default="INFO", description="日志级别")


@lru_cache(maxsize=1)
def get_settings() -> RagSettings:
    """获取（进程内缓存的）配置单例。

    Returns:
        RagSettings 实例。单元测试中可用 `get_settings.cache_clear()` 重置。
    """
    return RagSettings()


__all__ = ["RagSettings", "get_settings"]
