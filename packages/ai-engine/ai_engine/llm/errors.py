"""LLM 配置异常。

设计要点：
1. 继承统一 `RagError`，沿用契约层错误码，不新增错误码常量。
2. 配置错误不可重试，构建期快速失败，运行期由 `invoke_structured` 降级。
"""

from __future__ import annotations

from rag_contracts.errors import RagError


class LlmConfigError(RagError):
    """LLM 构建期配置错误。"""

    retryable = False


__all__ = ["LlmConfigError"]
