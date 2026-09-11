"""rag-contracts 契约自检（设计文档 T01 验收标准 3）。

覆盖：
1. 必填校验与条件必填（LAW_ARTICLE→law_code / WINDOW_SLICE→parent_section_id）
2. `chunk_id` 确定性（同输入 2 次结果相同）与 `text_hash` 指纹
3. 枚举闭合（SourceType 与 PreciseCitation.sourceType 取值一致）
4. `CitationOut` 与 `ai_engine.agents.auditor.PreciseCitation` 字段 1:1 对齐
5. Function Calling 规范符合 OpenAI strict 模式
6. 导出的 4 个 JSON Schema 可被校验器解析（无 jsonschema 时自动跳过）
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rag_contracts import (
    AgentRAGToolInput,
    AgentRAGToolResult,
    BBox,
    ChunkScores,
    CitationOut,
    ContractChunk,
    EMPTY_CONTEXT_HINT,
    FallbackReason,
    RAGQueryRequest,
    RAGQueryResponse,
    RetrievedDoc,
    RetrievalFilters,
    SourceType,
    TokenUsage,
    build_function_calling_spec,
    to_citation,
)
from rag_contracts.chunk import EMBEDDING_DIM
from rag_contracts.errors import (
    ERR_SCHEMA_VERSION_MISMATCH,
    RagTimeoutError,
    SchemaVersionMismatchError,
)

# ---------------------------------------------------------------------------
# 让测试能 import 既有 ai_engine（用于 PreciseCitation 对齐断言）
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_ENGINE = _REPO_ROOT / "packages" / "ai-engine"
if str(_AI_ENGINE) not in sys.path:
    sys.path.insert(0, str(_AI_ENGINE))

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "rag_contracts" / "schemas"


# ===========================================================================
# 辅助：构造一个合法的 ContractChunk
# ===========================================================================
def make_chunk(
    *,
    doc_id: str = "STD-PURCHASE-001",
    section_id: str = "6.2",
    source_type: SourceType = SourceType.STANDARD_CLAUSE,
    text: str = "买方逾期付款的，应按日支付合同总价千分之五的违约金。",
    **overrides: Any,
) -> ContractChunk:
    """构造合法 ContractChunk（自动补齐派生字段）。"""
    payload: dict[str, Any] = {
        "chunk_id": ContractChunk.make_chunk_id(doc_id, section_id),
        "doc_id": doc_id,
        "source_type": source_type,
        "chunk_type": "CLAUSE",
        "section_id": section_id,
        "section_path": "第六章/6.2",
        "level": 3,
        "seq_in_doc": 0,
        "title": "逾期付款违约金",
        "text": text,
        "text_hash": ContractChunk.compute_text_hash(text),
        "char_count": len(text),
        "token_count": max(1, len(text) // 2),
        "contract_type": "PURCHASE",
        "category": "PENALTY_TOO_HIGH",
        "tags": ["违约金", "付款"],
        "page_no": 3,
        "bbox": {
            "page": 3,
            "x": 72.0,
            "y": 120.5,
            "width": 451.0,
            "height": 60.0,
            "coordinate_system": "PDF_POINTS",
        },
    }
    payload.update(overrides)
    return ContractChunk(**payload)


# ===========================================================================
# 1. 基础校验
# ===========================================================================
def test_contract_chunk_roundtrip() -> None:
    """切片→payload→切片 的往返必须无损（chunk_id / text_hash 不变）。"""
    chunk = make_chunk()
    payload = chunk.to_payload()
    assert "extra" not in payload, "extra 不应进入 Qdrant payload（无法建索引）"
    assert payload["source_type"] == "STANDARD_CLAUSE"
    assert isinstance(payload["bbox"], dict), "bbox 必须被 dict 化"

    back = ContractChunk.from_payload(payload)
    assert back.chunk_id == chunk.chunk_id
    assert back.text_hash == chunk.text_hash
    assert back.bbox is not None and back.bbox.page == 3


def test_extra_forbid_rejects_typo() -> None:
    """拼错字段必须立即报错，不能静默吞掉（契约驱动第一道防线）。"""
    with pytest.raises(ValidationError) as exc:
        make_chunk(section_idd="6.2")  # type: ignore[call-arg]
    assert "extra_forbidden" in str(exc.value) or "extra" in str(exc.value).lower()


def test_text_hash_is_sha256_prefix16() -> None:
    """text_hash 必须是 sha256 前 16 位小写十六进制。"""
    h = ContractChunk.compute_text_hash("违约金")
    assert len(h) == 16
    assert h == ContractChunk.compute_text_hash("违约金"), "同文本必须同 hash"
    assert h != ContractChunk.compute_text_hash("违约金。")


def test_chunk_id_is_deterministic() -> None:
    """验收标准：同输入 2 次生成的 chunk_id 必须相同（幂等 upsert 的前提）。"""
    a = ContractChunk.make_chunk_id("DOC-1", "6.2", 0)
    b = ContractChunk.make_chunk_id("DOC-1", "6.2", 0)
    assert a == b
    assert ContractChunk.make_chunk_id("DOC-1", "6.2", 1) != a, "slice_idx 必须影响结果"
    assert ContractChunk.make_chunk_id("DOC-2", "6.2", 0) != a, "doc_id 必须影响结果"


# ===========================================================================
# 2. 条件必填
# ===========================================================================
def test_law_article_requires_law_code() -> None:
    """验收标准：LAW_ARTICLE 缺 law_code 必须报错。"""
    with pytest.raises(ValidationError) as exc:
        make_chunk(
            source_type=SourceType.LAW_ARTICLE,
            doc_id="民法典",
            section_id="第五百八十五条",
            chunk_type="ARTICLE",
        )
    assert "law_code" in str(exc.value)


def test_law_article_with_law_code_ok() -> None:
    """带上 law_code 后应通过，且 law_title/article_no 可溯源。"""
    chunk = make_chunk(
        source_type=SourceType.LAW_ARTICLE,
        doc_id="民法典",
        section_id="第五百八十五条",
        chunk_type="ARTICLE",
            law_code="民法典",
            law_title="中华人民共和国民法典",
            article_no="第五百八十五条",
            effective_date="2021-01-01",
            title=None,  # 清空 title 才能验证 law_title + article_no 这一级兜底
            contract_type=None,
            bbox=None,
            page_no=None,
        )
    assert chunk.law_code == "民法典"
    assert chunk.effective_date == date(2021, 1, 1)
    assert chunk.citation_title() == "中华人民共和国民法典 第五百八十五条"


def test_window_slice_requires_parent() -> None:
    """WINDOW_SLICE 缺 parent_section_id 必须报错。"""
    with pytest.raises(ValidationError) as exc:
        make_chunk(chunk_type="WINDOW_SLICE")
    assert "parent_section_id" in str(exc.value)


def test_embedding_dim_locked_to_1024() -> None:
    """embedding_dim 非 null 时必须等于 1024（项目锁定）。"""
    with pytest.raises(ValidationError) as exc:
        make_chunk(embedding_dim=768)
    assert "1024" in str(exc.value)
    assert make_chunk(embedding_dim=None).embedding_dim is None
    assert EMBEDDING_DIM == 1024


# ===========================================================================
# 3. 溯源映射：与既有 PreciseCitation 1:1
# ===========================================================================
def test_citation_matches_precise_citation_field_set() -> None:
    """验收标准：CitationOut 与 auditor.PreciseCitation 字段集完全相等。"""
    from ai_engine.agents.auditor import PreciseCitation  # type: ignore[import-not-found]

    expected = set(PreciseCitation.model_fields.keys())
    actual = set(CitationOut.model_fields.keys())
    assert actual == expected, (
        f"CitationOut 与 PreciseCitation 字段集不一致："
        f"RAG 独有={actual - expected}，缺失={expected - actual}"
    )


def test_citation_values_assignable_to_precise_citation() -> None:
    """更强的断言：值必须能被 PreciseCitation 直接接受（类型也对齐）。"""
    from ai_engine.agents.auditor import PreciseCitation  # type: ignore[import-not-found]

    chunk = make_chunk(
        source_type=SourceType.LAW_ARTICLE,
        doc_id="民法典",
        section_id="第五百八十五条",
        chunk_type="ARTICLE",
            law_code="民法典",
            law_title="中华人民共和国民法典",
            article_no="第五百八十五条",
            title=None,
            contract_type=None,
            bbox=None,
            page_no=None,
        )
    citation = to_citation(chunk, 0.87)
    # 关键：字段集 1:1，可直接构造（模拟 Agent 侧 citations=[...] 赋值）
    precise = PreciseCitation(**citation.model_dump())
    assert precise.sourceType == "LAW_ARTICLE"
    assert precise.sourceTitle == "中华人民共和国民法典 第五百八十五条"
    assert precise.relevanceScore == 0.87


def test_citation_truncates_long_text() -> None:
    """contentChunk 超 800 字必须截断并带省略号。"""
    chunk = make_chunk(text="违" * 5000)
    citation = to_citation(chunk, 1.5)  # 分数越界 → clamp 到 1.0
    assert len(citation.contentChunk) == 801
    assert citation.contentChunk.endswith("…")
    assert citation.relevanceScore == 1.0


def test_citation_title_fallback_chain() -> None:
    """title 缺失时逐级兜底，最终兜到 doc_id#section_id，保证非空。"""
    chunk = make_chunk(title=None, section_path="第六章/6.2")
    assert chunk.citation_title() == "第六章/6.2"
    bare = make_chunk(title=None, section_path="")
    assert bare.citation_title() == "STD-PURCHASE-001#6.2"


# ===========================================================================
# 4. 请求 / 响应契约
# ===========================================================================
def test_request_defaults_and_trace_id() -> None:
    """trace_id 自动生成；阈值沿用前作 0.35。"""
    req = RAGQueryRequest(query="违约金上限")
    assert req.trace_id, "trace_id 必须自动生成"
    assert req.score_threshold == 0.35
    assert req.top_k == 8
    assert RAGQueryRequest(query="x", trace_id="fixed-id").trace_id == "fixed-id"


def test_token_usage_add_recomputes_total() -> None:
    """TokenUsage.add() 必须累加分项并重算 total。"""
    usage = TokenUsage()
    usage.add(embedding_tokens=10, rerank_tokens=5)
    usage.add(prompt_tokens=100, completion_tokens=50)
    assert usage.total_tokens == 165
    usage.add(total_tokens=99999)  # 显式 total_tokens 必须被忽略
    assert usage.total_tokens == 165


def test_response_requires_core_fields() -> None:
    """响应契约的核心字段必填（trace_id / query）。"""
    with pytest.raises(ValidationError):
        RAGQueryResponse()  # type: ignore[call-arg]
    resp = RAGQueryResponse(trace_id="t1", query="q")
    assert resp.degraded is False
    assert resp.fallback_reason is FallbackReason.NONE
    assert resp.schema_version == "1.0.0"


def test_retrieved_doc_requires_scores_and_citation() -> None:
    """RetrievedDoc 必须带 scores 与 citation（供前端溯源）。"""
    chunk = make_chunk()
    doc = RetrievedDoc(
        chunk_id=chunk.chunk_id,
        doc_id=chunk.doc_id,
        source_type=chunk.source_type,
        chunk_type=chunk.chunk_type,
        section_id=chunk.section_id,
        text=chunk.text,
        scores=ChunkScores(final=0.9, rank=1),
        citation=to_citation(chunk, 0.9),
    )
    assert doc.citation.sourceType == SourceType.STANDARD_CLAUSE
    with pytest.raises(ValidationError):
        RetrievedDoc(chunk_id="c", doc_id="d", source_type="X",  # type: ignore[arg-type]
                     chunk_type="CLAUSE", section_id="s", text="t")


def test_filters_reject_unknown_field() -> None:
    """过滤条件同样禁止未知字段。"""
    with pytest.raises(ValidationError):
        RetrievalFilters(unknown_field=1)  # type: ignore[call-arg]


# ===========================================================================
# 5. Agent Tool 契约
# ===========================================================================
def test_function_calling_spec_is_openai_strict() -> None:
    """验收标准（T04 前置）：strict 模式下所有字段必须 required。"""
    spec = build_function_calling_spec()
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "retrieve_contract_knowledge"
    assert fn["strict"] is True

    params = fn["parameters"]
    assert params["additionalProperties"] is False
    props = set(params["properties"].keys())
    assert set(params["required"]) == props, "strict 模式下 required 必须覆盖全部字段"
    assert props == {"query", "contract_type", "source_types", "top_k", "reason"}
    # 必须无外部 $ref（OpenAI strict 对嵌套 $ref 支持不稳定）
    assert "$defs" not in json.dumps(params)


def test_agent_tool_input_optional_via_nullable() -> None:
    """可选性由联合类型表达，而非从 required 中移除。"""
    spec = build_function_calling_spec()
    ct = spec["function"]["parameters"]["properties"]["contract_type"]
    blob = json.dumps(ct)
    assert "null" in blob, "可选字段必须显式 nullable"


def test_agent_tool_result_to_message() -> None:
    """EMPTY 时必须返回兜底提示语，且状态可见。"""
    result = AgentRAGToolResult(
        status="EMPTY",
        context_block=EMPTY_CONTEXT_HINT,
        trace_id="t-1",
        warnings=["query 超长已截断"],
    )
    msg = result.to_tool_message()
    assert "[status=EMPTY]" in msg
    assert "【缺失标准对照】" in msg
    assert "[警告] query 超长已截断" in msg


def test_agent_tool_input_requires_query() -> None:
    """query 是唯一真正必填的入参。"""
    with pytest.raises(ValidationError):
        AgentRAGToolInput()  # type: ignore[call-arg]
    assert AgentRAGToolInput(query="违约金上限").top_k == 5


# ===========================================================================
# 6. 导出的 JSON Schema
# ===========================================================================
def test_exported_schema_files_exist() -> None:
    """4 个 JSON Schema 文件必须由导出脚本生成并存在。"""
    expected = [
        "contract_chunk.schema.json",
        "rag_query_request.schema.json",
        "rag_query_response.schema.json",
        "agent_rag_tool.schema.json",
    ]
    for name in expected:
        path = SCHEMAS_DIR / name
        assert path.exists(), f"缺少 Schema：{name}（请先跑 scripts/export_schemas.py）"
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"].startswith("https://json-schema.org/")
        assert schema["additionalProperties"] is False


def test_contract_chunk_schema_conditional_required() -> None:
    """导出的 Schema 必须含 allOf 条件必填与强制 required。"""
    schema = json.loads((SCHEMAS_DIR / "contract_chunk.schema.json").read_text(encoding="utf-8"))
    assert {"schema_version", "created_at", "chunk_id", "text"} <= set(schema["required"])
    allof_blob = json.dumps(schema.get("allOf", []))
    assert "LAW_ARTICLE" in allof_blob and "law_code" in allof_blob
    assert "WINDOW_SLICE" in allof_blob and "parent_section_id" in allof_blob


def test_exported_schemas_validatable() -> None:
    """用 jsonschema 校验器解析并校验真实样例（无该库时跳过）。"""
    jsonschema = pytest.importorskip("jsonschema")

    chunk_schema = json.loads(
        (SCHEMAS_DIR / "contract_chunk.schema.json").read_text(encoding="utf-8")
    )
    law = make_chunk(
        source_type=SourceType.LAW_ARTICLE,
        doc_id="民法典",
        section_id="第五百八十五条",
        chunk_type="ARTICLE",
        law_code="民法典",
        contract_type=None,
        bbox=None,
        page_no=None,
    )
    jsonschema.validate(law.model_dump(mode="json"), chunk_schema)

    # 负例：LAW_ARTICLE 缺 law_code 必须被 Schema 拒绝
    # 注意：JSON Schema 的 required 只校验"键存在"，因此要真正删键而非置 null
    bad = law.model_dump(mode="json")
    bad.pop("law_code")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, chunk_schema)

    req_schema = json.loads(
        (SCHEMAS_DIR / "rag_query_request.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(RAGQueryRequest(query="违约金").model_dump(mode="json"), req_schema)

    resp_schema = json.loads(
        (SCHEMAS_DIR / "rag_query_response.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(
        RAGQueryResponse(trace_id="t", query="q").model_dump(mode="json"), resp_schema
    )


# ===========================================================================
# 7. 异常体系
# ===========================================================================
def test_error_codes_and_retryable() -> None:
    """错误码与可重试标记必须正确（Agent 层据此决定是否重试）。"""
    err = RagTimeoutError("检索超时", trace_id="t-1")
    assert err.code == "CG-RAG-1004"
    assert err.retryable is True
    assert "CG-RAG-1004" in str(err)
    assert err.to_dict()["trace_id"] == "t-1"
    assert SchemaVersionMismatchError().code == ERR_SCHEMA_VERSION_MISMATCH


def test_bbox_requires_pdf_points() -> None:
    """BBox 坐标系必须与 doc_processor 对齐。"""
    bbox = BBox(page=1, x=0.0, y=0.0, width=10.0, height=20.0)
    assert bbox.coordinate_system == "PDF_POINTS"
    with pytest.raises(ValidationError):
        BBox(page=0, x=0.0, y=0.0, width=1.0, height=1.0)  # page 必须 >= 1
