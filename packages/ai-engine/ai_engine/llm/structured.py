"""结构化输出调用与降级链。

设计要点：
1. 首选 `function_calling`，避开 DeepSeek 不支持的 `json_schema` 响应格式。
2. 依次降级到 `json_object` 和纯提示词，运行期永不向节点抛异常。
3. 识别 reasoning 模型空 content 截断，并只放大重试一次。
"""

from __future__ import annotations

import json
import time
from typing import Any, TypeVar

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from cg_observability import get_logger, get_trace_id, log_event

_TModel = TypeVar("_TModel", bound=BaseModel)
_LOGGER = get_logger("ai_engine.llm.structured")


def _model_info(model: BaseChatModel) -> tuple[str, str]:
    raw_provider = getattr(model, "_llm_type", None)
    provider = raw_provider() if callable(raw_provider) else raw_provider
    model_name = (
        getattr(model, "model_name", None)
        or getattr(model, "model", None)
        or type(model).__name__
    )
    return str(provider or "unknown"), str(model_name)


def _message_content(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def _coerce_value(value: Any, schema: type[_TModel]) -> _TModel:
    if isinstance(value, schema):
        return value
    if isinstance(value, BaseModel):
        return schema.model_validate(value.model_dump())
    if value is None:
        raise ValueError("结构化输出为空")
    return schema.model_validate(value)


def _extract_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def _parse_message(message: Any, schema: type[_TModel]) -> _TModel:
    content = _message_content(message)
    if not content.strip():
        raise ValueError("模型返回空 content")
    return schema.model_validate_json(_extract_json_text(content))


def _attempt_function_calling(
    model: BaseChatModel,
    schema: type[_TModel],
    messages: list[BaseMessage],
) -> tuple[_TModel, Any]:
    try:
        runnable: Runnable[Any, Any] = model.with_structured_output(
            schema,
            method="function_calling",
            include_raw=True,
        )
    except TypeError:
        runnable = model.with_structured_output(
            schema,
            method="function_calling",
        )

    result = runnable.invoke(messages)
    raw = None
    parsed: Any = result
    if isinstance(result, dict) and "parsed" in result:
        raw = result.get("raw")
        parsing_error = result.get("parsing_error")
        if parsing_error is not None:
            raise parsing_error
        parsed = result.get("parsed")
        if parsed is None and raw is not None and not getattr(raw, "tool_calls", None):
            # 兼容模型直接返回 JSON 的情况；仍严格使用原 Schema 校验。
            return _parse_message(raw, schema), raw
    return _coerce_value(parsed, schema), raw


def _attempt_json_object(
    model: BaseChatModel,
    schema: type[_TModel],
    messages: list[BaseMessage],
) -> tuple[_TModel, Any]:
    bound = model.bind(response_format={"type": "json_object"})
    result = bound.invoke(messages)
    return _parse_message(result, schema), result


def _attempt_prompt_json(
    model: BaseChatModel,
    schema: type[_TModel],
    messages: list[BaseMessage],
) -> tuple[_TModel, Any]:
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    prompt = HumanMessage(
        content=(
            "仅返回一个符合以下 JSON Schema 的 JSON 对象，不要附加解释：\n"
            f"{schema_json}"
        )
    )
    result = model.invoke([*messages, prompt])
    return _parse_message(result, schema), result


def _usage_details(raw: Any) -> tuple[int | None, int | None, str]:
    response_metadata = getattr(raw, "response_metadata", {}) or {}
    usage_metadata = getattr(raw, "usage_metadata", {}) or {}
    token_usage = response_metadata.get("token_usage") or {}
    completion_details = token_usage.get("completion_tokens_details") or {}
    output_details = usage_metadata.get("output_token_details") or {}

    reasoning = (
        completion_details.get("reasoning_tokens")
        or output_details.get("reasoning")
        or output_details.get("reasoning_tokens")
    )
    total = (
        usage_metadata.get("total_tokens")
        or token_usage.get("total_tokens")
        or usage_metadata.get("output_tokens")
        or token_usage.get("completion_tokens")
    )
    finish_reason = str(
        response_metadata.get("finish_reason")
        or getattr(raw, "finish_reason", "")
        or ""
    )
    return (
        int(reasoning) if reasoning is not None else None,
        int(total) if total is not None else None,
        finish_reason,
    )


def _truncation_warning(model: BaseChatModel, raw: Any) -> str | None:
    if raw is None or _message_content(raw).strip():
        return None
    reasoning_tokens, _, finish_reason = _usage_details(raw)
    max_tokens = getattr(model, "max_tokens", None)
    if max_tokens is None:
        max_tokens = getattr(model, "max_completion_tokens", None)
    capped = (
        isinstance(max_tokens, int)
        and reasoning_tokens is not None
        and reasoning_tokens >= max_tokens
    )
    if finish_reason == "length" or capped:
        return (
            "[reasoning_truncated] content 为空且 reasoning tokens "
            f"触碰 max_tokens（reasoning={reasoning_tokens}, max_tokens={max_tokens}）"
        )
    return None


def _expanded_model(model: BaseChatModel) -> BaseChatModel:
    for field_name in ("max_tokens", "max_completion_tokens"):
        current = getattr(model, field_name, None)
        if isinstance(current, int) and current > 0:
            expanded = min(current * 2, 8192)
            try:
                updates: dict[str, Any] = {field_name: expanded}
                extra_body = getattr(model, "extra_body", None)
                if isinstance(extra_body, dict) and "max_tokens" in extra_body:
                    updates["extra_body"] = {**extra_body, "max_tokens": expanded}
                return model.model_copy(update=updates)
            except Exception:  # noqa: BLE001 - 任意自定义模型不支持复制时保持原实例
                return model
    return model


def _warning(stage: str, exc: Exception) -> str:
    return f"[{stage}] {type(exc).__name__}: {exc}"


def _json_mode(model: BaseChatModel) -> str:
    metadata = getattr(model, "metadata", None)
    if isinstance(metadata, dict):
        return str(metadata.get("cg_llm_json_mode", "json_mode"))
    return "json_mode"


def _log_degrade(
    *,
    stage: str,
    provider: str,
    model_name: str,
    trace_id: str,
    latency_ms: float,
    error: Exception,
) -> None:
    log_event(
        _LOGGER,
        "llm.invoke.degrade",
        level="WARNING",
        trace_id=trace_id,
        degrade_stage=stage,
        provider=provider,
        model=model_name,
        latency_ms=round(latency_ms, 2),
        error_type=type(error).__name__,
        error=str(error)[:300],
    )
    log_event(
        _LOGGER,
        "llm.structured.degrade",
        level="WARNING",
        trace_id=trace_id,
        degrade_stage=stage,
        provider=provider,
        model=model_name,
        latency_ms=round(latency_ms, 2),
        error_type=type(error).__name__,
        error=str(error)[:300],
    )


def _log_done(
    *,
    provider: str,
    model_name: str,
    trace_id: str,
    latency_ms: float,
    tokens: int | None,
    success: bool,
    degraded: bool,
) -> None:
    log_event(
        _LOGGER,
        "llm.invoke.done",
        trace_id=trace_id,
        provider=provider,
        model=model_name,
        tokens=tokens,
        latency_ms=round(latency_ms, 2),
        success=success,
        degraded=degraded,
    )
    if success:
        log_event(
            _LOGGER,
            "llm.structured.ok",
            trace_id=trace_id,
            provider=provider,
            model=model_name,
            tokens=tokens,
            latency_ms=round(latency_ms, 2),
            success=success,
            degraded=degraded,
        )


def invoke_structured(
    model: BaseChatModel,
    schema: type[_TModel],
    messages: list[BaseMessage],
    *,
    fallback: _TModel | None = None,
    trace_id: str | None = None,
) -> tuple[_TModel | None, list[str]]:
    """调用模型并解析为 schema，失败时返回 fallback 与 warnings。"""
    provider, model_name = _model_info(model)
    resolved_trace_id = trace_id or get_trace_id() or "-"
    started = time.perf_counter()
    warnings: list[str] = []

    log_event(
        _LOGGER,
        "llm.invoke.start",
        provider=provider,
        model=model_name,
        trace_id=resolved_trace_id,
    )

    try:
        value, raw = _attempt_function_calling(model, schema, messages)
        truncation = _truncation_warning(model, raw)
        if truncation:
            warnings.append(truncation)
            _log_degrade(
                stage="reasoning_truncated",
                provider=provider,
                model_name=model_name,
                trace_id=resolved_trace_id,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=RuntimeError(truncation),
            )
            retry_model = _expanded_model(model)
            if retry_model is not model:
                try:
                    value, retry_raw = _attempt_function_calling(
                        retry_model,
                        schema,
                        messages,
                    )
                    retry_warning = _truncation_warning(retry_model, retry_raw)
                    if retry_warning:
                        warnings.append(retry_warning)
                    _, tokens, _ = _usage_details(retry_raw)
                except Exception as exc:  # noqa: BLE001 - 运行期降级必须吞掉模型异常
                    warnings.append(_warning("reasoning_retry", exc))
                    _, tokens, _ = _usage_details(raw)
            else:
                _, tokens, _ = _usage_details(raw)
        else:
            _, tokens, _ = _usage_details(raw)

        _log_done(
            provider=provider,
            model_name=model_name,
            trace_id=resolved_trace_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens=tokens,
            success=True,
            degraded=bool(warnings),
        )
        return value, warnings
    except Exception as exc:  # noqa: BLE001 - structured 接口契约要求永不抛异常
        warnings.append(_warning("function_calling", exc))
        _log_degrade(
            stage="function_calling",
            provider=provider,
            model_name=model_name,
            trace_id=resolved_trace_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=exc,
        )

    if _json_mode(model) != "none":
        try:
            value, raw = _attempt_json_object(model, schema, messages)
            _, tokens, _ = _usage_details(raw)
            _log_done(
                provider=provider,
                model_name=model_name,
                trace_id=resolved_trace_id,
                latency_ms=(time.perf_counter() - started) * 1000,
                tokens=tokens,
                success=True,
                degraded=True,
            )
            return value, warnings
        except Exception as exc:  # noqa: BLE001 - structured 接口契约要求永不抛异常
            warnings.append(_warning("json_object", exc))
            _log_degrade(
                stage="json_object",
                provider=provider,
                model_name=model_name,
                trace_id=resolved_trace_id,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=exc,
            )
    else:
        warnings.append("[json_object] skipped because CG_LLM_JSON_MODE=none")

    try:
        value, raw = _attempt_prompt_json(model, schema, messages)
        _, tokens, _ = _usage_details(raw)
        _log_done(
            provider=provider,
            model_name=model_name,
            trace_id=resolved_trace_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            tokens=tokens,
            success=True,
            degraded=True,
        )
        return value, warnings
    except Exception as exc:  # noqa: BLE001 - structured 接口契约要求永不抛异常
        warnings.append(_warning("prompt_json", exc))
        _log_degrade(
            stage="prompt_json",
            provider=provider,
            model_name=model_name,
            trace_id=resolved_trace_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=exc,
        )

    _log_done(
        provider=provider,
        model_name=model_name,
        trace_id=resolved_trace_id,
        latency_ms=(time.perf_counter() - started) * 1000,
        tokens=None,
        success=False,
        degraded=True,
    )
    return fallback, warnings


__all__ = ["invoke_structured"]
