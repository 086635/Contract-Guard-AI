"""LLM 工厂与进程内缓存。

设计要点：
1. 全仓只有本模块构造在线 ChatModel。
2. 缓存键只使用可哈希标量，避免 BaseSettings 不可哈希。
3. 配置错误在构建期快速失败，不把异常留给运行期节点。
"""

from __future__ import annotations

import threading
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from cg_observability import get_logger, get_trace_id, log_event

from .errors import LlmConfigError
from .offline import build_offline_model
from .settings import LlmSettings, get_llm_settings

_PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
}
_CACHE: dict[tuple[Any, ...], BaseChatModel] = {}
_CACHE_LOCK = threading.RLock()
_LOGGER = get_logger("ai_engine.llm.factory")


def _missing_api_key(provider: str) -> LlmConfigError:
    return LlmConfigError(
        f"provider={provider!r} 缺少 CG_LLM_API_KEY",
        detail={"provider": provider},
    )


def build_chat_model(
    settings: LlmSettings | None = None,
    *,
    temperature: float | None = None,
    trace_id: str | None = None,
) -> BaseChatModel:
    """构造 ChatModel，供业务节点统一调用。"""
    resolved = settings or get_llm_settings()
    model_temperature = (
        resolved.temperature if temperature is None else float(temperature)
    )
    provider = resolved.provider

    if provider == "offline":
        model = build_offline_model()
    elif provider in {"openai", "deepseek", "ollama"}:
        if not resolved.model.strip():
            raise LlmConfigError("CG_LLM_MODEL 不得为空")

        api_key = (resolved.api_key or "").strip()
        if provider in {"openai", "deepseek"} and not api_key:
            raise _missing_api_key(provider)
        if provider == "ollama" and not api_key:
            api_key = "ollama"

        base_url = resolved.base_url or _PROVIDER_BASE_URLS.get(provider)
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=resolved.model,
            temperature=model_temperature,
            max_tokens=resolved.max_tokens,  # type: ignore[call-arg]
            timeout=resolved.timeout_s,
            max_retries=resolved.max_retries,
            api_key=SecretStr(api_key),
            base_url=base_url,
            metadata={
                "cg_llm_provider": provider,
                "cg_llm_model": resolved.model,
                "cg_llm_json_mode": resolved.json_mode,
                "cg_llm_allow_external_data": resolved.allow_external_data,
            },
        )
    else:
        raise LlmConfigError(
            f"不支持的 CG_LLM_PROVIDER：{provider!r}",
            detail={"provider": provider},
        )

    log_event(
        _LOGGER,
        "llm.build",
        trace_id=trace_id or get_trace_id() or "-",
        provider=provider,
        model=resolved.model,
        temperature=model_temperature,
        max_tokens=resolved.max_tokens,
        json_mode=resolved.json_mode,
        allow_external_data=resolved.allow_external_data,
    )
    return model


def _cache_key(settings: LlmSettings) -> tuple[Any, ...]:
    return (
        settings.provider,
        settings.model,
        settings.temperature,
        settings.max_tokens,
        settings.timeout_s,
        settings.max_retries,
        settings.api_key or "",
        settings.base_url or "",
        settings.json_mode,
        settings.allow_external_data,
    )


def get_chat_model(
    role: str = "default",
    settings: LlmSettings | None = None,
) -> BaseChatModel:
    """按角色取模型，并使用进程内缓存复用同一配置。"""
    resolved = settings or get_llm_settings()
    key = _cache_key(resolved)
    with _CACHE_LOCK:
        model = _CACHE.get(key)
        if model is None:
            model = build_chat_model(resolved)
            _CACHE[key] = model
    return model


def clear_llm_cache() -> None:
    """清空模型缓存。"""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = ["build_chat_model", "clear_llm_cache", "get_chat_model"]
