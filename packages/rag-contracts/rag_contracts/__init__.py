"""rag-contracts：RAG 检索模块契约层（设计文档 RAG_MODULE_DESIGN.md 第二章）。

本包**零重型依赖**（仅 pydantic），可被 ai-engine / rag-core / api-gateway / worker 任意引用。

三层内容：
1. **数据契约**：`ContractChunk` / `RAGQueryRequest` / `RAGQueryResponse`
2. **工具契约**：`AgentRAGToolInput` / `AgentRAGToolResult` / `build_function_calling_spec()`
3. **端口抽象**：`VectorStorePort` / `SparseStorePort` / `EmbedderPort` /
   `RerankerPort` / `TokenCounterPort`

⚠️ 契约冻结提醒：本包字段一旦发布即冻结。
变更流程：先改设计文档 → 再改本包模型 → 重跑 `scripts/export_schemas.py` → 递增 `schema_version`。
"""

from __future__ import annotations

from .agent_tool import (
    EMPTY_CONTEXT_HINT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    AgentRAGToolInput,
    AgentRAGToolResult,
    build_function_calling_spec,
)
from .chunk import (
    BBox,
    ContractChunk,
    EMBEDDING_DIM,
    SCHEMA_VERSION,
)
from .citation import clamp_score, to_citation, truncate_text
from .enums import (
    ChunkType,
    ContractType,
    FallbackReason,
    RetrieveStrategy,
    SourceType,
)
from .errors import (
    ERR_CONTRACT_VALIDATION,
    ERR_EMBED_FAILED,
    ERR_EMPTY_RECALL,
    ERR_QUERY_TOO_LONG,
    ERR_RERANK_FAILED,
    ERR_SCHEMA_VERSION_MISMATCH,
    ERR_STORE_UNAVAILABLE,
    ERR_TIMEOUT,
    ContractValidationError,
    EmbedFailedError,
    QueryTooLongError,
    RagError,
    RagTimeoutError,
    RerankFailedError,
    SchemaVersionMismatchError,
    StoreUnavailableError,
    TransientRagError,
)
from .ports import (
    EmbedderPort,
    RerankerPort,
    SparseHit,
    SparseStorePort,
    TokenCounterPort,
    VectorHit,
    VectorRecord,
    VectorStorePort,
)
from .query import (
    DEFAULT_RRF_K,
    DEFAULT_SCORE_THRESHOLD,
    ChunkScores,
    CitationOut,
    RAGQueryRequest,
    RAGQueryResponse,
    RetrievedDoc,
    RetrievalFilters,
    TokenUsage,
)

__version__ = "1.0.0"

__all__ = [
    # ---- 常量 ----
    "DEFAULT_RRF_K",
    "DEFAULT_SCORE_THRESHOLD",
    "EMBEDDING_DIM",
    "EMPTY_CONTEXT_HINT",
    "ERR_CONTRACT_VALIDATION",
    "ERR_EMBED_FAILED",
    "ERR_EMPTY_RECALL",
    "ERR_QUERY_TOO_LONG",
    "ERR_RERANK_FAILED",
    "ERR_SCHEMA_VERSION_MISMATCH",
    "ERR_STORE_UNAVAILABLE",
    "ERR_TIMEOUT",
    "SCHEMA_VERSION",
    "TOOL_DESCRIPTION",
    "TOOL_NAME",
    "__version__",
    # ---- 枚举 ----
    "ChunkType",
    "ContractType",
    "FallbackReason",
    "RetrieveStrategy",
    "SourceType",
    # ---- 数据契约 ----
    "BBox",
    "ChunkScores",
    "CitationOut",
    "ContractChunk",
    "RAGQueryRequest",
    "RAGQueryResponse",
    "RetrievedDoc",
    "RetrievalFilters",
    "TokenUsage",
    # ---- 工具契约 ----
    "AgentRAGToolInput",
    "AgentRAGToolResult",
    "build_function_calling_spec",
    # ---- 溯源映射 ----
    "clamp_score",
    "to_citation",
    "truncate_text",
    # ---- 端口 ----
    "EmbedderPort",
    "RerankerPort",
    "SparseHit",
    "SparseStorePort",
    "TokenCounterPort",
    "VectorHit",
    "VectorRecord",
    "VectorStorePort",
    # ---- 异常 ----
    "ContractValidationError",
    "EmbedFailedError",
    "QueryTooLongError",
    "RagError",
    "RagTimeoutError",
    "RerankFailedError",
    "SchemaVersionMismatchError",
    "StoreUnavailableError",
    "TransientRagError",
]
