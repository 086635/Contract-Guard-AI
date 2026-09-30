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


class ProductionConfigError(ValueError):
    """生产配置门禁失败；只携带字段名与原因，不携带 secret。"""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


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
    vector_store: Literal["inmemory", "qdrant", "milvus"] = Field(
        default="inmemory",
        description="向量库实现。inmemory=零依赖（默认/单测）；qdrant/milvus=真实向量库",
    )
    qdrant_mode: Literal["server", "local_memory", "local_path"] = Field(
        default="local_memory",
        description="Qdrant 运行模式：server=连服务 / local_memory=内存 / local_path=本地磁盘",
    )
    qdrant_url: str = Field(
        default="http://localhost:6333", description="Qdrant 服务地址"
    )
    qdrant_local_path: str = Field(
        default="./data/qdrant", description="本地磁盘模式的数据目录"
    )
    qdrant_api_key: str | None = Field(default=None, description="Qdrant Cloud API Key")
    # ---- Milvus（T-A2 新增；备选适配器，见 1-2 / 1.C5）----
    milvus_mode: Literal["server", "lite"] = Field(
        default="server",
        description="server=连服务（推荐，Windows 只能用它）；lite=本地文件（Linux/macOS）",
    )
    milvus_uri: str = Field(
        default="http://localhost:19530",
        description="server 模式为服务地址；lite 模式为本地 .db 文件路径",
    )
    milvus_token: str = Field(
        default="", description="认证串 user:password 或 API Key；为空表示无认证"
    )
    milvus_db_name: str = Field(default="default", description="Milvus 数据库名")
    milvus_timeout_s: float = Field(
        default=5.0, ge=0.1, description="Milvus 请求超时（秒）"
    )
    collection: str = Field(default="contract_clauses", description="集合名")
    vector_dim: int = Field(default=1024, ge=1, description="向量维度（bge-m3 = 1024）")

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    embed_mode: Literal["http", "hash"] = Field(
        default="hash",
        description="hash=离线确定性伪向量（默认，零依赖）；http=TEI / OpenAI 兼容 API",
    )
    embed_base_url: str = Field(
        default="http://localhost:8080", description="Embedding 服务地址"
    )
    embed_api_key: str = Field(default="none", description="Embedding API Key")
    embed_model: str = Field(default="BAAI/bge-m3", description="Embedding 模型名")
    embed_batch_size: int = Field(default=32, ge=1, description="批量向量化大小")
    embed_timeout_ms: int = Field(
        default=3000, ge=100, description="单次请求超时（毫秒）"
    )
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
    rerank_model: str = Field(
        default="BAAI/bge-reranker-v2-m3", description="重排模型名"
    )
    rerank_http_url: str = Field(default="", description="HTTP 重排服务地址")
    rerank_api_key: str = Field(
        default="none", description="HTTP 重排服务 Bearer Token"
    )
    rerank_path: str = Field(default="/rerank", description="HTTP 重排接口路径")
    rerank_timeout_ms: int = Field(
        default=3000, ge=100, description="重排单次超时（毫秒）"
    )
    rerank_threshold: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description=(
            "（T-A7 接线）精排分下限：**仅当精排真正生效**时叠加到相关性下限；"
            "noop / 未配置 URL 时为 None（不叠加，保证离线默认路径行为不变）"
        ),
    )
    rerank_top_n: int = Field(
        default=5,
        ge=1,
        le=20,
        description="（T-A7 接线）重排后保留条数的**服务级默认值**，请求未显式传时生效",
    )

    # ------------------------------------------------------------------
    # 预算、降级与默认值
    # ------------------------------------------------------------------
    max_context_tokens: int = Field(
        default=3000, ge=256, le=32000, description="上下文 Token 预算"
    )
    max_query_chars: int = Field(
        default=2000,
        ge=100,
        description=(
            "（T-A7 接线）消费方：Agent 工具层「防线 1 · 超长拦截」"
            "（`ai_engine/agents/tools.py`，T-B4）；rag-core 不实现切分（分层红线）"
        ),
    )
    timeout_ms: int = Field(
        default=3000,
        ge=100,
        le=30000,
        description=(
            "整体超时（毫秒）。消费方①：Qdrant 客户端超时（factory.build_vector_store）；"
            "消费方②（T-A7）：retrieve() 的整体预算语义"
        ),
    )
    max_retries: int = Field(
        default=3,
        ge=1,
        description=(
            "（T-A7 接线）消费方：Agent 工具层「防线 2 · 超时重试」"
            "（`ai_engine/agents/tools.py`，T-B4）；rag-core 不实现重试（分层红线）"
        ),
    )
    score_threshold: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="相关性下限（沿用前作 0.35）。（T-A7 接线）作为服务级默认阈值，请求未显式传时生效",
    )
    strategy: RetrieveStrategy = Field(
        default=RetrieveStrategy.DENSE_ONLY,
        description=(
            "（T-A7 接线）服务级默认检索策略：**仅当请求未显式传 strategy 时生效**。"
            "注意它与 RAGQueryRequest.strategy 的默认值（HYBRID_RRF）差异是**刻意的**："
            "请求侧是「能力声明」，本配置是「当前实际启用」，不要统一二者"
        ),
    )

    # ------------------------------------------------------------------
    # 缓存与日志
    # ------------------------------------------------------------------
    cache_ttl_s: int = Field(
        default=300,
        ge=0,
        description="热 Query 缓存 TTL（秒）。⚠️ **已登记未实现**：当前无缓存实现，保留字段以免丢失需求记录",
    )
    # T-A7：**删除 log_level 字段**。日志级别实际由 `cg_observability/logger.py` 直接读环境变量
    # `CG_RAG_LOG_LEVEL`（os.environ.get），本字段是重复声明、改了不生效。
    # 反向接线（让 cg_observability 消费 RagSettings）会破坏分层（可观测层在 rag-core 之下），
    # 故这里只能删，不能接。

    def validate_production(self) -> None:
        """生产环境拒绝离线 embedding 与 noop reranker。"""
        if self.embed_mode == "hash":
            raise ProductionConfigError(
                "CG_RAG_EMBED_MODE",
                "production requires non-hash embedding",
            )
        if not self.rerank_enabled or self.rerank_mode == "noop":
            raise ProductionConfigError(
                "CG_RAG_RERANK_MODE",
                "production requires a real reranker",
            )


@lru_cache(maxsize=1)
def get_settings() -> RagSettings:
    """获取（进程内缓存的）配置单例。

    Returns:
        RagSettings 实例。单元测试中可用 `get_settings.cache_clear()` 重置。
    """
    return RagSettings()


__all__ = ["ProductionConfigError", "RagSettings", "get_settings"]
