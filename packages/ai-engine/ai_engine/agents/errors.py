"""Agent 运行期异常。"""

from __future__ import annotations

from rag_contracts.errors import RagError


class RewriteUnavailableError(RagError):
    """改写模型或 Prompt 不可用。"""

    retryable = True


__all__ = ["RewriteUnavailableError"]
