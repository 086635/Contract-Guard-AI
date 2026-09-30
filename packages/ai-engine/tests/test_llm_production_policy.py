"""C54 真实 LLM provider 与外部数据开关策略测试。"""

from __future__ import annotations

from typing import Any

import langchain_openai
import pytest

from ai_engine.llm.factory import build_chat_model
from ai_engine.llm.settings import LlmSettings
from rag_core.config import ProductionConfigError


def _settings(**overrides: Any) -> LlmSettings:
    values: dict[str, Any] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com/v1",
        "allow_external_data": True,
    }
    values.update(overrides)
    return LlmSettings(_env_file=None, **values)


def test_production_rejects_offline() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _settings(provider="offline").validate_production()

    assert exc_info.value.field == "CG_LLM_PROVIDER"


def test_external_data_switch() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _settings(allow_external_data=False).validate_production()

    assert exc_info.value.field == "CG_LLM_ALLOW_EXTERNAL_DATA"

    _settings(allow_external_data=True).validate_production()


@pytest.mark.parametrize(
    ("provider", "expected_base_url"),
    [
        ("deepseek", "https://api.deepseek.com/v1"),
        ("ollama", "http://localhost:11434/v1"),
    ],
)
def test_provider_switch_forwards_metadata(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    expected_base_url: str,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    build_chat_model(
        _settings(
            provider=provider,
            api_key="test-key" if provider == "deepseek" else None,
            base_url=None,
        )
    )

    assert captured["base_url"] == expected_base_url
    assert captured["metadata"] == {
        "cg_llm_provider": provider,
        "cg_llm_model": "deepseek-chat",
        "cg_llm_json_mode": "json_mode",
        "cg_llm_allow_external_data": True,
    }
