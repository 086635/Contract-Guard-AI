"""离线桩聊天模型。

设计要点：
1. 默认离线路径不依赖网络或 API Key。
2. `with_structured_output` 按 Pydantic JSON Schema 生成最小合法对象。
3. 生成算法保持确定性，供图编排与单测重复调用。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """解析本地 JSON Pointer 引用。"""
    if not ref.startswith("#/"):
        return {}
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part.replace("~1", "/").replace("~0", "~"))
    return node if isinstance(node, dict) else {}


def _minimal_value(node: dict[str, Any], root: dict[str, Any]) -> Any:
    """按单个 JSON Schema 节点生成最小合法值。"""
    if "$ref" in node:
        return _minimal_value(_resolve_ref(str(node["$ref"]), root), root)
    if "const" in node:
        return copy.deepcopy(node["const"])
    if node.get("enum"):
        return copy.deepcopy(node["enum"][0])
    if "default" in node:
        return copy.deepcopy(node["default"])

    for union_key in ("anyOf", "oneOf"):
        options = node.get(union_key)
        if isinstance(options, list) and options:
            null_option = [
                item
                for item in options
                if isinstance(item, dict) and item.get("type") == "null"
            ]
            if null_option:
                return None
            first = options[0]
            return _minimal_value(first, root) if isinstance(first, dict) else None

    all_of = node.get("allOf")
    if isinstance(all_of, list) and all_of:
        merged: dict[str, Any] = {}
        for item in all_of:
            if not isinstance(item, dict):
                continue
            resolved = _resolve_ref(str(item["$ref"]), root) if "$ref" in item else item
            value = _minimal_value(resolved, root)
            if isinstance(value, dict):
                merged.update(value)
        if merged:
            return merged

    schema_type = node.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "null")

    if schema_type == "object" or "properties" in node:
        properties = node.get("properties", {})
        required = node.get("required", [])
        result: dict[str, Any] = {}
        if isinstance(properties, dict) and isinstance(required, list):
            for field_name, field_schema in properties.items():
                if not isinstance(field_schema, dict):
                    continue
                union_options = field_schema.get("anyOf") or field_schema.get("oneOf")
                nullable = isinstance(union_options, list) and any(
                    isinstance(item, dict) and item.get("type") == "null"
                    for item in union_options
                )
                if field_name in required or nullable:
                    result[field_name] = _minimal_value(field_schema, root)
        return result

    if schema_type == "array":
        item_schema = node.get("items", {})
        min_items = int(node.get("minItems", 0) or 0)
        if min_items <= 0:
            return []
        item_value = (
            _minimal_value(item_schema, root) if isinstance(item_schema, dict) else None
        )
        return [copy.deepcopy(item_value) for _ in range(min_items)]

    if schema_type == "string":
        formats = {
            "date": "1970-01-01",
            "date-time": "1970-01-01T00:00:00Z",
            "email": "a@example.com",
            "time": "00:00:00",
            "uri": "https://example.com",
            "uuid": "00000000-0000-0000-0000-000000000000",
        }
        return formats.get(str(node.get("format", "")), "")

    if schema_type == "integer":
        value = int(node.get("minimum", 0) or 0)
        exclusive = node.get("exclusiveMinimum")
        if exclusive is not None and value <= int(exclusive):
            value = int(exclusive) + 1
        return value

    if schema_type == "number":
        value = float(node.get("minimum", 0.0) or 0.0)
        exclusive = node.get("exclusiveMinimum")
        if exclusive is not None and value <= float(exclusive):
            value = float(exclusive) + 1.0
        return value

    if schema_type == "boolean":
        return False

    return None


def minimal_json_for(schema: type[BaseModel]) -> str:
    """生成符合 Pydantic schema 的最小 JSON 字符串。"""
    schema_json = schema.model_json_schema()
    value = _minimal_value(schema_json, schema_json)
    return json.dumps(value, ensure_ascii=False)


class OfflineChatModel(BaseChatModel):
    """确定性离线聊天模型。"""

    @property
    def _llm_type(self) -> str:
        return "offline"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="{}"))])

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise TypeError("离线桩只支持 Pydantic BaseModel schema")

        def _invoke(_: Any) -> Any:
            payload = minimal_json_for(schema)
            parsed = schema.model_validate_json(payload)
            if include_raw:
                return {
                    "raw": AIMessage(content=payload),
                    "parsed": parsed,
                    "parsing_error": None,
                }
            return parsed

        return RunnableLambda(_invoke)


def build_offline_model() -> BaseChatModel:
    """构造离线桩模型。"""
    return OfflineChatModel()


__all__ = ["build_offline_model", "minimal_json_for"]
