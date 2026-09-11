#!/usr/bin/env python
"""从 Pydantic 模型**单向导出** JSON Schema（禁止手改生成的 JSON）。

用法（仓库根目录）：
    python packages/rag-contracts/scripts/export_schemas.py
    python packages/rag-contracts/scripts/export_schemas.py --check   # 仅校验是否过期

设计文档 §2.1.1 的导出对齐说明：
- `ContractChunk` 的 `schema_version` / `created_at` 在 Pydantic 侧有默认值（可省），
  但上线必须带，因此导出后需强制并入 `required`（共 12 项）。
- `embedding_dim` 未用 `const`（允许 None 表示"尚未向量化"），维度锁定由 Pydantic 校验器保证。
- 条件必填（LAW_ARTICLE→law_code、WINDOW_SLICE→parent_section_id）由 `allOf` 表达，
  Pydantic 不会自动生成，导出后手工追加。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 让脚本在任意工作目录下都能 import rag_contracts（无需先 pip install）
# ---------------------------------------------------------------------------
_PKG_ROOT = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from rag_contracts.agent_tool import AgentRAGToolResult  # noqa: E402
from rag_contracts.chunk import ContractChunk  # noqa: E402
from rag_contracts.query import RAGQueryRequest, RAGQueryResponse  # noqa: E402

SCHEMAS_DIR = _PKG_ROOT / "rag_contracts" / "schemas"

JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_PREFIX = "https://contract-guard.ai/schemas"

# 需要强制并入 required 的字段（Pydantic 因有默认值而未标记为必填，但契约要求上线必带）
_FORCED_REQUIRED: dict[str, tuple[str, ...]] = {
    "ContractChunk": ("schema_version", "created_at"),
    "RAGQueryRequest": ("query",),
    "RAGQueryResponse": (
        "trace_id",
        "query",
        "retrieved_docs",
        "degraded",
        "fallback_reason",
        "token_usage",
        "took_ms",
    ),
    "AgentRAGToolResult": (
        "status",
        "context_block",
        "citations",
        "trace_id",
        "warnings",
        "fallback_reason",
    ),
}

# ContractChunk 的条件必填：与 §2.1.1 的 allOf 完全一致（Pydantic 无法自动生成）
_CONTRACT_CHUNK_ALLOF: list[dict[str, Any]] = [
    {
        "if": {
            "properties": {"source_type": {"const": "LAW_ARTICLE"}},
            "required": ["source_type"],
        },
        "then": {
            "required": ["law_code"],
            "description": "source_type=LAW_ARTICLE 时 law_code 必填（契约硬约束）。",
        },
    },
    {
        "if": {
            "properties": {"chunk_type": {"const": "WINDOW_SLICE"}},
            "required": ["chunk_type"],
        },
        "then": {
            "required": ["parent_section_id"],
            "description": "滑动窗口子块必须指明父 section，保证溯源可回溯到完整条款。",
        },
    },
]

_DESCRIPTIONS: dict[str, str] = {
    "ContractChunk": (
        "合同与法条切片后的结构化存储单元。同一结构同时写入 Qdrant payload、"
        "BM25 文档表与 PostgreSQL，保证三处 chunk_id 完全一致。"
    ),
    "RAGQueryRequest": "RAG 检索请求契约。请求即配置：阈值、策略、预算全在请求里。",
    "RAGQueryResponse": "RAG 检索响应契约。自带 citation / token_usage / stage_latency_ms。",
    "AgentRAGToolResult": "retrieve_contract_knowledge 工具的返回契约（Function Calling 出参）。",
}

# 导出目标：模型 → 文件名
_EXPORT_TARGETS: list[tuple[type[Any], str]] = [
    (ContractChunk, "contract_chunk.schema.json"),
    (RAGQueryRequest, "rag_query_request.schema.json"),
    (RAGQueryResponse, "rag_query_response.schema.json"),
    (AgentRAGToolResult, "agent_rag_tool.schema.json"),
]


def build_schema(model: type[Any]) -> dict[str, Any]:
    """从 Pydantic 模型生成最终的 JSON Schema（含后处理）。

    Args:
        model: Pydantic v2 模型类。

    Returns:
        可直接被 jsonschema 校验器使用的 Schema 字典。
    """
    schema = model.model_json_schema(mode="validation")
    title = str(schema.get("title", model.__name__))

    schema["$schema"] = JSON_SCHEMA_DRAFT
    schema["$id"] = f"{SCHEMA_ID_PREFIX}/{title.lower()}.schema.json"
    schema["title"] = title
    schema["description"] = _DESCRIPTIONS.get(title, "")
    # extra="forbid" 的模型 Pydantic 已输出 false，这里兜底保证一致
    schema["additionalProperties"] = False

    required = set(schema.get("required", []))
    required.update(_FORCED_REQUIRED.get(title, ()))
    schema["required"] = sorted(required)

    if title == "ContractChunk":
        schema["allOf"] = _CONTRACT_CHUNK_ALLOF

    return schema


def export_all(*, check_only: bool = False) -> int:
    """导出全部 Schema 到 `rag_contracts/schemas/`。

    Args:
        check_only: True 时只比对磁盘内容是否与模型一致（用于 CI 防漂移），不写文件。

    Returns:
        退出码：0 成功；1 表示 check 模式发现漂移。
    """
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    drifted: list[str] = []

    for model, filename in _EXPORT_TARGETS:
        schema = build_schema(model)
        text = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        path = SCHEMAS_DIR / filename

        if check_only:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                drifted.append(filename)
            continue

        path.write_text(text, encoding="utf-8")
        n_props = len(schema.get("properties", {}))
        print(f"[ok] {filename:<34} properties={n_props:<3} required={len(schema['required'])}")

    if check_only:
        if drifted:
            print(f"[drift] 以下 Schema 与模型不一致，请重跑导出脚本：{', '.join(drifted)}")
            return 1
        print("[ok] 所有 JSON Schema 与 Pydantic 模型一致，无漂移")
        return 0

    print(f"\n导出完成 → {SCHEMAS_DIR}")
    return 0


def main() -> int:
    """命令行入口。

    Returns:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="导出 RAG 契约 JSON Schema")
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅校验磁盘上的 Schema 是否与 Pydantic 模型一致（CI 用）",
    )
    args = parser.parse_args()
    return export_all(check_only=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
