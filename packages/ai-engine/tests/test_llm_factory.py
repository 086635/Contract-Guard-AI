"""LLM 适配层单测（T-B1 / C2）。

设计要点：
1. 所有用例离线运行，不调用真实模型或外网。
2. 用可编程假模型验证三级降级和 reasoning 截断重试。
3. 同时锁住工厂缓存、provider 预设与配置透传。
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import langchain_openai
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, Field

from ai_engine.llm import (
    LlmConfigError,
    LlmSettings,
    build_chat_model,
    clear_llm_cache,
    get_chat_model,
    get_llm_settings,
    invoke_structured,
)
from ai_engine.llm.offline import build_offline_model, minimal_json_for
from ai_engine.llm.structured import _expanded_model


class _Choice(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"


class _Payload(BaseModel):
    name: str
    count: int
    tags: list[str]
    choice: _Choice
    note: str | None = None


def _payload_json() -> str:
    return json.dumps(
        {
            "name": "ok",
            "count": 1,
            "tags": [],
            "choice": "PRIMARY",
            "note": None,
        },
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def _isolate_llm_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """隔离真实 `.env`、外部模型配置与进程内缓存。"""
    for key in list(os.environ):
        if key.startswith("CG_LLM_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    clear_llm_cache()
    yield
    clear_llm_cache()


class _FirstThenSecondModel(BaseChatModel):
    calls: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "first-then-second"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=_payload_json()))]
        )

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        self.calls.append(f"function_calling:{kwargs.get('method', '')}")

        def _fail(_: Any) -> Any:
            raise RuntimeError("function calling failed")

        return RunnableLambda(_fail)

    def bind(self, **kwargs: Any) -> Runnable[Any, Any]:
        self.calls.append("json_object")

        def _succeed(_: Any) -> AIMessage:
            return AIMessage(content=_payload_json())

        return RunnableLambda(_succeed)


class _ThirdStageModel(BaseChatModel):
    calls: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(
        default_factory=lambda: {"cg_llm_json_mode": "json_mode"}
    )

    @property
    def _llm_type(self) -> str:
        return "third-stage"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append("prompt_json")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=_payload_json()))]
        )

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        self.calls.append("function_calling")

        def _fail(_: Any) -> Any:
            raise RuntimeError("function calling failed")

        return RunnableLambda(_fail)

    def bind(self, **kwargs: Any) -> Runnable[Any, Any]:
        self.calls.append("json_object")

        def _fail(_: Any) -> Any:
            raise RuntimeError("json object failed")

        return RunnableLambda(_fail)


class _AlwaysFailModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "always-fail"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="not json"))]
        )

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        def _fail(_: Any) -> Any:
            raise RuntimeError("function calling failed")

        return RunnableLambda(_fail)

    def bind(self, **kwargs: Any) -> Runnable[Any, Any]:
        def _fail(_: Any) -> Any:
            raise RuntimeError("json object failed")

        return RunnableLambda(_fail)


class _TruncatingModel(BaseChatModel):
    max_tokens: int = 16
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "truncating"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=""))])

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        def _invoke(_: Any) -> dict[str, Any]:
            self.calls += 1
            parsed = _Payload.model_validate_json(_payload_json())
            if self.max_tokens <= 16:
                raw = AIMessage(
                    content="",
                    response_metadata={
                        "finish_reason": "length",
                        "token_usage": {
                            "completion_tokens": 16,
                            "completion_tokens_details": {
                                "reasoning_tokens": 16,
                            },
                        },
                    },
                )
            else:
                raw = AIMessage(
                    content=_payload_json(),
                    usage_metadata={
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "total_tokens": 3,
                    },
                )
            return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return RunnableLambda(_invoke)


def test_default_settings_are_offline() -> None:
    settings = LlmSettings(_env_file=None)
    assert settings.provider == "offline"
    assert settings.temperature == 0.0
    assert settings.max_tokens == 2048
    assert get_llm_settings().provider == "offline"


def test_settings_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CG_LLM_MAX_TOKENS", "128")
    assert LlmSettings(_env_file=None).max_tokens == 128


def test_build_offline_model_is_base_chat_model() -> None:
    model = build_chat_model(LlmSettings(_env_file=None))
    assert isinstance(model, BaseChatModel)
    assert model._llm_type == "offline"


def test_offline_minimal_json_covers_schema_shapes() -> None:
    payload = json.loads(minimal_json_for(_Payload))
    assert payload == {
        "name": "",
        "count": 0,
        "tags": [],
        "choice": "PRIMARY",
        "note": None,
    }


def test_offline_structured_output_roundtrip() -> None:
    model = build_offline_model()
    value, warnings = invoke_structured(
        model,
        _Payload,
        [HumanMessage(content="离线测试")],
    )
    assert isinstance(value, _Payload)
    assert value.name == ""
    assert value.count == 0
    assert value.tags == []
    assert value.choice is _Choice.PRIMARY
    assert value.note is None
    assert warnings == []


def test_get_chat_model_caches_by_role_and_clear() -> None:
    settings = LlmSettings(_env_file=None)
    first = get_chat_model("risk", settings)
    second = get_chat_model("risk", settings)
    assert first is second

    clear_llm_cache()
    third = get_chat_model("risk", settings)
    assert third is not first


def test_temperature_override_changes_cache_key() -> None:
    base = LlmSettings(_env_file=None)
    warm = base.model_copy(update={"temperature": 0.7})
    assert get_chat_model("risk", base) is not get_chat_model("risk", warm)


def test_openai_without_key_raises_config_error_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _forbidden_constructor(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("缺 Key 时不得构造 ChatOpenAI")

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _forbidden_constructor)
    settings = LlmSettings(
        _env_file=None,
        provider="openai",
        api_key=None,
    )
    with pytest.raises(LlmConfigError) as caught:
        build_chat_model(settings)
    assert caught.value.retryable is False


def test_deepseek_without_key_raises_config_error() -> None:
    with pytest.raises(LlmConfigError):
        build_chat_model(
            LlmSettings(
                _env_file=None,
                provider="deepseek",
                api_key="",
            )
        )


def test_provider_presets_and_settings_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    settings = LlmSettings(
        _env_file=None,
        provider="deepseek",
        model="deepseek-flash",
        api_key="test-key",
        temperature=0.2,
        max_tokens=4096,
        timeout_s=12.5,
        max_retries=3,
        json_mode="json_mode",
    )

    build_chat_model(settings)

    assert captured["base_url"] == "https://api.deepseek.com/v1"
    assert captured["model"] == "deepseek-flash"
    assert captured["temperature"] == 0.2
    assert captured["max_tokens"] == 4096
    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 3
    assert captured["metadata"] == {
        "cg_llm_provider": "deepseek",
        "cg_llm_model": "deepseek-flash",
        "cg_llm_json_mode": "json_mode",
        "cg_llm_allow_external_data": False,
    }


def test_ollama_uses_local_preset_and_placeholder_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    build_chat_model(
        LlmSettings(
            _env_file=None,
            provider="ollama",
            model="qwen2.5:7b",
            api_key=None,
        )
    )
    assert captured["base_url"] == "http://localhost:11434/v1"
    assert captured["api_key"].get_secret_value() == "ollama"


@pytest.mark.parametrize("provider", ["deepseek", "ollama", "openai"])
def test_actual_sdk_request_respects_provider_output_limit(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """走真实 SDK 序列化，避免仅检查构造参数漏掉字段改名。"""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "mock-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    real_constructor = langchain_openai.ChatOpenAI
    with httpx.Client(
        transport=httpx.MockTransport(respond), trust_env=False
    ) as client:

        def build_mock_transport_model(**kwargs: Any) -> BaseChatModel:
            kwargs["http_client"] = client
            return real_constructor(**kwargs)

        monkeypatch.setattr(langchain_openai, "ChatOpenAI", build_mock_transport_model)
        model = build_chat_model(
            LlmSettings(
                _env_file=None,
                provider=provider,
                model="test-model",
                api_key="test-key",
                base_url="https://model.invalid/v1",
                max_tokens=32,
                max_retries=0,
            )
        )
        assert model.invoke("test").content == "ok"
        expanded = _expanded_model(model)
        assert expanded.invoke("test").content == "ok"

    if provider in {"deepseek", "ollama"}:
        assert [item["max_tokens"] for item in requests] == [32, 64]
        assert model.extra_body["max_tokens"] == 32
    else:
        assert [item["max_completion_tokens"] for item in requests] == [32, 64]
        assert all("max_tokens" not in item for item in requests)
    assert (
        all(item.get("thinking") == {"type": "disabled"} for item in requests)
        if provider == "deepseek"
        else all("thinking" not in item for item in requests)
    )


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]", "model.example"])
def test_only_loopback_ollama_bypasses_system_proxy(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    created: list[tuple[str, bool]] = []
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: created.append(("sync", kwargs["trust_env"]))
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: created.append(("async", kwargs["trust_env"])),
    )
    monkeypatch.setattr(
        langchain_openai, "ChatOpenAI", lambda **kwargs: captured.update(kwargs)
    )
    build_chat_model(
        LlmSettings(
            _env_file=None,
            provider="ollama",
            base_url=f"http://{host}:11434/v1",
        )
    )
    assert created == (
        [("sync", False), ("async", False)] if host != "model.example" else []
    )


def test_expansion_preserves_provider_options_and_original_model() -> None:
    model = langchain_openai.ChatOpenAI(
        model="test-model",
        api_key="test-key",
        max_tokens=6000,
        extra_body={"max_tokens": 6000, "thinking": {"type": "disabled"}},
    )
    expanded = _expanded_model(model)
    assert expanded.max_tokens == expanded.extra_body["max_tokens"] == 8192
    assert expanded.extra_body["thinking"] == {"type": "disabled"}
    assert model.extra_body["max_tokens"] == model.max_tokens == 6000


@pytest.mark.parametrize(
    "first_content,expected_calls", [(_payload_json(), 1), ("{}", 2)]
)
def test_tool_response_json_is_validated_without_unnecessary_network_retry(
    monkeypatch: pytest.MonkeyPatch,
    first_content: str,
    expected_calls: int,
) -> None:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "mock-json",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": first_content
                            if len(requests) == 1
                            else _payload_json(),
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    constructor = langchain_openai.ChatOpenAI
    with httpx.Client(
        transport=httpx.MockTransport(respond), trust_env=False
    ) as client:

        def build_with_mock(**kwargs: Any) -> BaseChatModel:
            kwargs["http_client"] = client
            return constructor(**kwargs)

        monkeypatch.setattr(langchain_openai, "ChatOpenAI", build_with_mock)
        model = build_chat_model(
            LlmSettings(
                _env_file=None,
                provider="deepseek",
                api_key="test-key",
                base_url="https://model.invalid/v1",
                max_retries=0,
            )
        )
        value, warnings = invoke_structured(
            model, _Payload, [HumanMessage(content="test JSON")]
        )
    assert value is not None and value.name == "ok"
    assert len(requests) == expected_calls
    assert bool(warnings) is (expected_calls == 2)
    if expected_calls == 2:
        assert requests[1]["response_format"] == {"type": "json_object"}


def test_structured_prefers_function_calling_then_json_object() -> None:
    model = _FirstThenSecondModel()
    value, warnings = invoke_structured(
        model,
        _Payload,
        [HumanMessage(content="x")],
    )
    assert isinstance(value, _Payload)
    assert model.calls == ["function_calling:function_calling", "json_object"]
    assert len(warnings) == 1
    assert "function_calling" in warnings[0]


def test_structured_falls_back_to_prompt_json() -> None:
    model = _ThirdStageModel()
    value, warnings = invoke_structured(
        model,
        _Payload,
        [HumanMessage(content="x")],
    )
    assert isinstance(value, _Payload)
    assert model.calls == ["function_calling", "json_object", "prompt_json"]
    assert len(warnings) == 2
    assert "json_object" in warnings[1]


def test_json_mode_none_skips_json_object_stage() -> None:
    model = _ThirdStageModel(metadata={"cg_llm_json_mode": "none"})
    value, warnings = invoke_structured(
        model,
        _Payload,
        [HumanMessage(content="x")],
    )
    assert isinstance(value, _Payload)
    assert model.calls == ["function_calling", "prompt_json"]
    assert any("CG_LLM_JSON_MODE=none" in warning for warning in warnings)


def test_invoke_structured_degrades_without_raising() -> None:
    fallback = _Payload(
        name="fallback",
        count=1,
        tags=[],
        choice=_Choice.SECONDARY,
    )
    value, warnings = invoke_structured(
        _AlwaysFailModel(),
        _Payload,
        [HumanMessage(content="x")],
        fallback=fallback,
    )
    assert value is fallback
    assert len(warnings) == 3
    assert "prompt_json" in warnings[-1]


def test_empty_content_with_reasoning_truncation_is_retried_once() -> None:
    model = _TruncatingModel()
    value, warnings = invoke_structured(
        model,
        _Payload,
        [HumanMessage(content="x")],
    )
    assert isinstance(value, _Payload)
    assert any("reasoning_truncated" in warning for warning in warnings)


def test_structured_source_uses_function_calling() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "ai_engine" / "llm" / "structured.py"
    ).read_text(encoding="utf-8")
    assert 'method="function_calling"' in source
    assert source.index('method="function_calling"') < source.index(
        'response_format={"type": "json_object"}'
    )


def test_deepseek_clients_keep_tls_verification_and_system_proxy(monkeypatch):
    import ssl

    created = []
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: created.append(kwargs))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: created.append(kwargs))
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", lambda **kwargs: None)
    build_chat_model(LlmSettings(_env_file=None, provider="deepseek", model="test", api_key="test-key", timeout_s=25))
    assert len(created) == 2
    for options in created:
        assert isinstance(options["verify"], ssl.SSLContext)
        assert options["verify"].verify_mode == ssl.CERT_REQUIRED
        assert options["verify"].check_hostname is True
        assert options["trust_env"] is True
        assert options["timeout"] == 25
