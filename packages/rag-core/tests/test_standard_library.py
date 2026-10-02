"""C70 标准条款库检索/索引行为（只走 rag_core / ai_engine 公开入口）。

设计要点：
1. 断言标准条款命中**只经唯一检索入口**（`RagTool` → `RAGService.retrieve()`），不新建第二套检索。
2. 断言停用（从索引删除）后不再命中，且重复全量重建（确定性 chunk_id upsert）幂等。
3. 本文件不改 `rag_core` 实现，只验证 C70 依赖的公开接缝。
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

from cg_observability import TokenCounter
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType
from rag_contracts.ports import VectorRecord
from rag_core.adapters import HashEmbedder, InMemoryVectorStore, NoopReranker
from rag_core.config import RagSettings
from rag_core.service import RAGService

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_ENGINE = _REPO_ROOT / "packages" / "ai-engine"
if str(_AI_ENGINE) not in sys.path:
    sys.path.insert(0, str(_AI_ENGINE))

# 说明：此 import 必须在上方 sys.path 注入之后（测试路径引导，不是漏写）。
from ai_engine.agents.tools import AgentRAGToolResult, RagTool  # noqa: E402

_PENALTY_ID = UUID("11111111-1111-1111-1111-111111111111")
_CONFIDENTIALITY_ID = UUID("22222222-2222-2222-2222-222222222222")

_PENALTY_TEXT = "买方逾期付款的，应按日支付合同总价千分之五的违约金。"
_CONFIDENTIALITY_TEXT = "双方对商业秘密负有保密义务，保密期限为合同终止后三年。"


def build_standard_chunk(clause_id: UUID, title: str, text: str) -> ContractChunk:
    """构造与 C70 `StandardClauseIndexer` 同形的标准条款切片。"""
    doc_id = f"std:{clause_id}"
    section_id = f"std-{clause_id}"
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id(doc_id, section_id, 0),
        doc_id=doc_id,
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id=section_id,
        section_path=title,
        seq_in_doc=0,
        title=title,
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=max(1, round(len(text) / 1.5)),
        level=3,
        contract_type=ContractType.PURCHASE,
        embedding_model="hash-test",
    )


def build_library(
    chunks: list[ContractChunk],
) -> tuple[RagTool, InMemoryVectorStore, HashEmbedder]:
    """用唯一 RAG 入口装配检索工具 + 同一个内存向量集合。"""
    embedder = HashEmbedder(dim=1024, model_name="hash-test")
    store = InMemoryVectorStore()
    store.ensure_collection(embedder.dim)
    store.upsert(
        [
            VectorRecord(chunk=chunk, vector=vector)
            for chunk, vector in zip(
                chunks,
                embedder.embed_batch([chunk.text for chunk in chunks]),
                strict=True,
            )
        ]
    )
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
        dense_top=20,
        default_max_context_tokens=3000,
    )
    tool = RagTool(service=service, settings=RagSettings(_env_file=None))
    return tool, store, embedder


def _search(tool: RagTool, text: str) -> AgentRAGToolResult:
    return tool(
        text,
        source_types=[SourceType.STANDARD_CLAUSE],
        contract_type=ContractType.PURCHASE,
        top_k=3,
    )


def test_standard_clause_search_uses_existing_tool() -> None:
    """标准条款检索必须经唯一 RAG 入口命中（source_type=STANDARD_CLAUSE）。"""
    chunk = build_standard_chunk(_PENALTY_ID, "逾期付款违约金", _PENALTY_TEXT)
    tool, store, _ = build_library([chunk])

    result = _search(tool, _PENALTY_TEXT)

    assert result.status == "OK"
    assert result.citations, "唯一检索入口应当命中刚入库的标准条款"
    top = result.citations[0]
    assert top.sourceType == SourceType.STANDARD_CLAUSE
    assert _PENALTY_TEXT in top.contentChunk
    assert store.count() == 1


def test_disabled_clause_not_retrieved() -> None:
    """停用（从索引删除）后不再命中生产检索；启用条目不受影响。"""
    enabled = build_standard_chunk(_PENALTY_ID, "逾期付款违约金", _PENALTY_TEXT)
    disabled = build_standard_chunk(
        _CONFIDENTIALITY_ID, "保密义务", _CONFIDENTIALITY_TEXT
    )
    tool, store, _ = build_library([enabled, disabled])

    before = _search(tool, _CONFIDENTIALITY_TEXT)
    assert any(_CONFIDENTIALITY_TEXT in item.contentChunk for item in before.citations)

    assert store.delete_by_doc(disabled.doc_id) == 1

    after = _search(tool, _CONFIDENTIALITY_TEXT)
    assert not any(_CONFIDENTIALITY_TEXT in item.contentChunk for item in after.citations)
    assert store.count() == 1
    assert store.delete_by_doc(enabled.doc_id) == 1


def test_rebuild_index_idempotent() -> None:
    """重建 = 确定性 chunk_id upsert：重复执行不产生孤儿向量。"""
    chunk = build_standard_chunk(_PENALTY_ID, "逾期付款违约金", _PENALTY_TEXT)
    tool, store, embedder = build_library([chunk])
    assert store.count() == 1

    rebuilt = build_standard_chunk(_PENALTY_ID, "逾期付款违约金", _PENALTY_TEXT)
    assert rebuilt.chunk_id == chunk.chunk_id
    store.upsert([VectorRecord(chunk=rebuilt, vector=embedder.embed_one(rebuilt.text))])

    assert store.count() == 1, "重复重建不应产生重复向量"
    result = _search(tool, _PENALTY_TEXT)
    matched = [
        item for item in result.citations if _PENALTY_TEXT in item.contentChunk
    ]
    assert len(matched) == 1
