"""RAG 模块错误码与异常体系（RAG_MODULE_DESIGN.md §2.5）。

设计要点：
1. 所有异常继承 `RagError`，携带统一 `code`（`CG-RAG-xxxx`）与 `retryable` 标记，
   调用方（如 Agent 工具层的 `_with_timeout_retry`）据此判断是否重试。
2. 降级不是错误：`EMPTY_RECALL`（CG-RAG-2001）是**握手信号**而非异常，
   因此不在此定义异常类，仅保留错误码常量供日志归因。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 错误码常量（日志埋点与 API 响应统一引用，避免魔法字符串）
# ---------------------------------------------------------------------------
ERR_QUERY_TOO_LONG: str = "CG-RAG-1001"
ERR_EMBED_FAILED: str = "CG-RAG-1002"
ERR_STORE_UNAVAILABLE: str = "CG-RAG-1003"
ERR_TIMEOUT: str = "CG-RAG-1004"
ERR_EMPTY_RECALL: str = "CG-RAG-2001"  # 非异常：握手信号
ERR_RERANK_FAILED: str = "CG-RAG-2002"
ERR_CONTRACT_VALIDATION: str = "CG-RAG-3001"
ERR_SCHEMA_VERSION_MISMATCH: str = "CG-RAG-3002"


class RagError(Exception):
    """RAG 模块异常基类。

    Attributes:
        code: 统一错误码（CG-RAG-xxxx）。
        retryable: 是否可重试（超时/网络类为 True，校验类为 False）。
        trace_id: 全链路追踪 ID，便于日志串联。
        detail: 结构化上下文（如失败的 doc_id / 原始异常类型）。
    """

    code: str = "CG-RAG-0000"
    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        trace_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """初始化异常。

        Args:
            message: 人类可读的错误描述。
            trace_id: 全链路追踪 ID。
            detail: 额外结构化上下文。
        """
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.trace_id = trace_id
        self.detail: dict[str, Any] = detail or {}

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入日志/API 响应的字典。"""
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "trace_id": self.trace_id,
            "detail": self.detail,
        }

    def __str__(self) -> str:
        """形如 `[CG-RAG-1004] 检索超时 (trace_id=xxx)`。"""
        suffix = f" (trace_id={self.trace_id})" if self.trace_id else ""
        return f"[{self.code}] {self.message}{suffix}"


class TransientRagError(RagError):
    """可重试的瞬时错误（网络抖动 / 5xx / 连接池耗尽）。

    设计文档把 `EmbedFailedError`（1002）与 `RerankFailedError`（2002）
    的降级路径都收敛到本类，因此允许在构造时覆盖 `code`。
    """

    code: str = ERR_EMBED_FAILED
    retryable: bool = True

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        trace_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        """初始化，可覆盖错误码以区分 embed / rerank 场景。"""
        super().__init__(message, trace_id=trace_id, detail=detail)
        if code:
            self.code = code


class QueryTooLongError(RagError):
    """query 超过硬上限（> 20000 字符），拒绝并提示缩短。"""

    code: str = ERR_QUERY_TOO_LONG
    retryable: bool = False


class EmbedFailedError(TransientRagError):
    """向量化失败（HTTP 5xx / 超时 / 返回维度不符）。"""

    code: str = ERR_EMBED_FAILED
    retryable: bool = True


class StoreUnavailableError(TransientRagError):
    """向量库 / BM25 不可用（连接失败 / 集合缺失）。"""

    code: str = ERR_STORE_UNAVAILABLE
    retryable: bool = True


class RagTimeoutError(TransientRagError):
    """整体或分段超时。"""

    code: str = ERR_TIMEOUT
    retryable: bool = True


class RerankFailedError(TransientRagError):
    """重排失败（模型加载失败 / 推理异常），降级为融合分。"""

    code: str = ERR_RERANK_FAILED
    retryable: bool = True


class ContractValidationError(RagError):
    """契约校验失败（Pydantic / JSON Schema），不可重试。"""

    code: str = ERR_CONTRACT_VALIDATION
    retryable: bool = False


class SchemaVersionMismatchError(RagError):
    """`schema_version` 不匹配，拒绝写入并提示迁移。"""

    code: str = ERR_SCHEMA_VERSION_MISMATCH
    retryable: bool = False


__all__ = [
    "ERR_CONTRACT_VALIDATION",
    "ERR_EMBED_FAILED",
    "ERR_EMPTY_RECALL",
    "ERR_QUERY_TOO_LONG",
    "ERR_RERANK_FAILED",
    "ERR_SCHEMA_VERSION_MISMATCH",
    "ERR_STORE_UNAVAILABLE",
    "ERR_TIMEOUT",
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
