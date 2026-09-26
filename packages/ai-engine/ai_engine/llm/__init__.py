"""LLM 适配层公开入口。

**唯一模型构造点**：业务节点只能经由 `build_chat_model` / `get_chat_model`
获取模型；本包不包含 Prompt、节点或图编排逻辑。
"""

from __future__ import annotations

from .errors import LlmConfigError
from .factory import build_chat_model, clear_llm_cache, get_chat_model
from .settings import LlmSettings, get_llm_settings
from .structured import invoke_structured

__all__ = [
    "LlmConfigError",
    "LlmSettings",
    "build_chat_model",
    "clear_llm_cache",
    "get_chat_model",
    "get_llm_settings",
    "invoke_structured",
]
