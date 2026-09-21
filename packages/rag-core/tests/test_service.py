"""rag-core 自检（设计文档 T02 验收标准）。

覆盖：
1. 切片：不跨条款红线、超长切窗、层级字段、统计口径
2. HashEmbedder：确定性、维度、相似度单调性
3. InMemoryVectorStore：幂等 upsert、过滤、删除
4. RAGService：召回命中、阈值降级、Token 与分段耗时、citation 可赋值
5. 配置默认值：离线可跑（inmemory + hash + noop）
6. InMemory 与 Qdrant(local_memory) 结果一致性（无 qdrant-client 时跳过）
7. T-A0-2：稀疏路独占候选由 `SparseHit.payload` 还原，不再被丢弃
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cg_observability import TokenCounter
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import (
    ChunkType,
    ContractType,
    FallbackReason,
    RetrieveStrategy,
    SourceType,
)
from rag_contracts.ports import SparseHit, VectorRecord
from rag_contracts.query import RAGQueryRequest, RetrievalFilters
from rag_core.adapters import HashEmbedder, InMemoryVectorStore, NoopReranker
from rag_core.config import RagSettings
from rag_core.context_builder import ContextBuilder
from rag_core.factory import (
    build_embedder,
    build_reranker,
    build_service_from_settings,
    build_vector_store,
)
from rag_core.service import RAGService

# ---------------------------------------------------------------------------
# PreciseCitation 对齐断言（与 ai_engine 侧保持 1:1）
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_ENGINE = _REPO_ROOT / "packages" / "ai-engine"
if str(_AI_ENGINE) not in sys.path:
    sys.path.insert(0, str(_AI_ENGINE))

# 说明：以下两个 import 未置于文件顶部是**刻意为之**——它们（及间接依赖）必须先让
# 上方 sys.path 注入生效才能导入，属测试用的路径引导，不是遗漏。
from rag_core.chunking import SimpleChunker, SourceDoc  # noqa: E402
from rag_core.chunking.simple_chunker import _CLAUSE_HEAD_RE  # noqa: E402


# ===========================================================================
# 辅助
# ===========================================================================
def make_doc(
    doc_id: str = "DOC-1",
    text: str = "第一条 标的\n甲方向乙方采购货物。\n第二条 价款\n合同总价为人民币壹佰万元。",
    **overrides: Any,
) -> SourceDoc:
    """构造源文档。"""
    payload: dict[str, Any] = {
        "doc_id": doc_id,
        "source_type": SourceType.STANDARD_CLAUSE,
        "text": text,
        "contract_type": ContractType.PURCHASE,
        "category": "PAYMENT_TERM_RISK",
    }
    payload.update(overrides)
    return SourceDoc(**payload)


def build_service(chunks: list[ContractChunk]) -> tuple[RAGService, InMemoryVectorStore]:
    """用给定切片组装一个可查询的服务。

    Args:
        chunks: 待入库切片。

    Returns:
        (service, store)。
    """
    embedder = HashEmbedder(dim=64, model_name="hash-test")
    store = InMemoryVectorStore()
    store.ensure_collection(embedder.dim)
    records = [
        VectorRecord(chunk=c, vector=v)
        for c, v in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    store.upsert(records)
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
        dense_top=20,
        default_max_context_tokens=3000,
    )
    return service, store


# ===========================================================================
# 1. 切片
# ===========================================================================
def test_split_blocks_by_clause_number() -> None:
    """按编号切块：每个块以编号开头，数量正确。"""
    blocks = SimpleChunker.split_blocks(make_doc().text)
    assert [b[0] for b in blocks] == ["第一条", "第二条"]
    assert blocks[0][1] == "标的"
    assert "甲方向乙方采购货物。" in blocks[0][2]


def test_chunk_does_not_cross_clause_boundary() -> None:
    """红线：任一 chunk 的正文最多只含一个条款编号锚点。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True), max_chars=600)
    chunks = chunker.chunk(make_doc())
    assert len(chunks) == 2
    for chunk in chunks:
        hits = [ln for ln in chunk.text.splitlines() if _CLAUSE_HEAD_RE.match(ln.strip())]
        assert len(hits) == 1, f"切片跨越了条款边界：{chunk.text!r}"


def test_long_block_becomes_window_slice_with_parent() -> None:
    """超长块切成 WINDOW_SLICE，且必须带 parent_section_id（契约硬约束）。"""
    long_body = "第三条 保密\n" + "乙方应对甲方商业秘密保密。" * 40
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True), max_chars=200)
    chunks = chunker.chunk(make_doc(text=long_body))

    slices = [c for c in chunks if c.chunk_type is ChunkType.WINDOW_SLICE]
    assert slices, "超长块未产生窗口子块"
    for sl in slices:
        assert sl.parent_section_id == "第三条"
        assert sl.section_id.startswith("第三条#")
        assert len(sl.text) <= 200


def test_chunk_stats_and_derived_fields() -> None:
    """派生字段：text_hash / char_count / token_count / chunk_id 确定性。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks, stats = chunker.chunk_with_stats(make_doc())
    assert stats.chunk_count == len(chunks) == 2
    assert stats.avg_chars > 0 and stats.max_chars >= stats.min_chars
    assert stats.total_tokens == sum(c.token_count for c in chunks)
    for chunk in chunks:
        assert chunk.text_hash == ContractChunk.compute_text_hash(chunk.text)
        assert chunk.char_count == len(chunk.text)
        assert chunk.chunk_id == ContractChunk.make_chunk_id(chunk.doc_id, chunk.section_id, 0)


def test_law_article_chunks_carry_law_code() -> None:
    """法条切片必须带 law_code，否则违反契约（Pydantic 会拒绝）。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    doc = make_doc(
        doc_id="民法典",
        source_type=SourceType.LAW_ARTICLE,
        law_code="民法典",
        law_title="中华人民共和国民法典",
        contract_type=None,
        text="第五百八十五条 违约金\n当事人可以约定一方违约时支付一定数额的违约金。",
    )
    chunks = chunker.chunk(doc)
    assert chunks and chunks[0].law_code == "民法典"
    assert chunks[0].chunk_type is ChunkType.ARTICLE
    assert chunks[0].article_no == "第五百八十五条"
    # 标题来自编号行后的文字，优先级最高（见 ContractChunk.citation_title 兜底链）
    assert chunks[0].title == "违约金"
    assert chunks[0].citation_title() == "违约金"


def test_source_doc_from_dict() -> None:
    """JSONL 行 → SourceDoc 的枚举/日期转换。"""
    doc = SourceDoc.from_dict(
        {
            "doc_id": "X",
            "source_type": "LAW_ARTICLE",
            "text": "第一条 测试",
            "contract_type": "NDA",
            "effective_date": "2021-01-01",
            "tags": ["a"],
        }
    )
    assert doc.source_type is SourceType.LAW_ARTICLE
    assert doc.contract_type is ContractType.NDA
    assert doc.effective_date is not None and doc.effective_date.isoformat() == "2021-01-01"


# ===========================================================================
# 2. HashEmbedder
# ===========================================================================
def test_hash_embedder_is_deterministic() -> None:
    """同文本必须同向量（跨进程也要一致，故用 blake2b 而非内置 hash）。"""
    embedder = HashEmbedder(dim=128)
    assert embedder.dim == 128
    assert embedder.model_name == "hash-trick-v1"
    a = embedder.embed_one("违约金上限")
    b = embedder.embed_one("违约金上限")
    assert a == b
    assert len(a) == 128


def test_hash_embedder_is_l2_normalized() -> None:
    """输出必须是单位向量（否则余弦不可比）。"""
    vec = HashEmbedder(dim=64).embed_one("甲方逾期付款")
    norm = sum(x * x for x in vec) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_hash_embedder_similarity_ordering() -> None:
    """共享词越多相似度越高（保证召回排序有意义）。"""
    embedder = HashEmbedder(dim=256)
    from rag_core.adapters.inmemory_store import cosine

    base = embedder.embed_one("买方逾期付款的违约金")
    near = embedder.embed_one("买方逾期付款的违约金上限")
    far = embedder.embed_one("劳动保护与社会保险缴纳")
    assert cosine(base, near) > cosine(base, far)


def test_hash_embedder_batch_matches_single() -> None:
    """embed_batch 与 embed_one 结果必须完全一致。"""
    embedder = HashEmbedder(dim=64)
    texts = ["第一条 标的", "第二条 价款"]
    assert embedder.embed_batch(texts) == [embedder.embed_one(t) for t in texts]


# ===========================================================================
# 3. InMemoryVectorStore
# ===========================================================================
def test_store_upsert_is_idempotent() -> None:
    """同一 chunk_id 重复写入只保留一条（幂等 upsert）。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    embedder = HashEmbedder(dim=64)
    store = InMemoryVectorStore()
    store.ensure_collection(64)
    vectors = embedder.embed_batch([c.text for c in chunks])
    records = [VectorRecord(chunk=c, vector=v) for c, v in zip(chunks, vectors)]

    assert store.upsert(records) == 2
    assert store.upsert(records) == 2
    assert store.count() == 2


def test_store_search_respects_filters() -> None:
    """过滤条件生效：source_types / doc_ids / contract_type。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    std = chunker.chunk(make_doc(doc_id="DOC-STD"))
    law = chunker.chunk(
        make_doc(
            doc_id="DOC-LAW",
            source_type=SourceType.LAW_ARTICLE,
            law_code="民法典",
            contract_type=None,
            text="第五百八十五条 违约金\n当事人可以约定违约金。",
        )
    )
    embedder = HashEmbedder(dim=64)
    store = InMemoryVectorStore()
    store.ensure_collection(64)
    all_chunks = std + law
    store.upsert(
        [
            VectorRecord(chunk=c, vector=v)
            for c, v in zip(all_chunks, embedder.embed_batch([c.text for c in all_chunks]))
        ]
    )

    vec = embedder.embed_one("违约金")
    assert len(store.search(vec, 10)) == 3
    only_law = store.search(
        vec, 10, RetrievalFilters(source_types=[SourceType.LAW_ARTICLE])
    )
    assert all(h.payload["source_type"] == "LAW_ARTICLE" for h in only_law)
    assert len(store.search(vec, 10, RetrievalFilters(doc_ids=["DOC-STD"]))) == 2


def test_store_delete_by_doc() -> None:
    """按 doc_id 删除。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    embedder = HashEmbedder(dim=64)
    store = InMemoryVectorStore()
    store.ensure_collection(64)
    store.upsert(
        [
            VectorRecord(chunk=c, vector=v)
            for c, v in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
        ]
    )
    assert store.delete_by_doc("DOC-1") == 2
    assert store.count() == 0
    assert store.health() is True


# ===========================================================================
# 4. RAGService
# ===========================================================================
def test_retrieve_returns_expected_top1() -> None:
    """验收：top1 命中预期切片，且分数、citation、Token 统计齐全。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(
        make_doc(
            doc_id="STD-1",
            text=(
                "第一条 标的\n甲方向乙方采购服务器设备。\n"
                "第二条 逾期付款违约金\n买方逾期付款的，应按日支付合同总价千分之五的违约金。\n"
                "第三条 保密\n双方对商业秘密负有保密义务。\n"
                "第四条 争议解决\n争议提交合同签订地人民法院诉讼解决。"
            ),
        )
    )
    service, store = build_service(chunks)
    assert store.count() == 4

    response = service.retrieve(
        RAGQueryRequest(
            query="买方逾期付款的违约金",
            top_k=3,
            strategy=RetrieveStrategy.DENSE_ONLY,
            score_threshold=0.25,
        )
    )
    assert response.retrieved_docs, "应当有召回"
    top = response.retrieved_docs[0]
    assert top.section_id == "第二条"
    assert 0.0 <= top.scores.final <= 1.0
    assert top.citation.sourceType == SourceType.STANDARD_CLAUSE
    assert "违约金" in top.citation.contentChunk
    assert response.context_block.startswith("[1] (")
    assert response.context_tokens > 0
    assert response.token_usage.embedding_tokens > 0
    assert response.token_usage.total_tokens >= response.token_usage.embedding_tokens
    assert set(response.stage_latency_ms) >= {"embed", "dense", "build"}
    assert response.total_candidates == 4


def test_retrieve_citation_assignable_to_precise_citation() -> None:
    """响应的 citation 必须能被既有 PreciseCitation 直接消费。"""
    from ai_engine.agents.auditor import (
        PreciseCitation,  # type: ignore[import-not-found]
    )

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    service, _ = build_service(chunks)
    response = service.retrieve(
        RAGQueryRequest(
            query="合同总价", strategy=RetrieveStrategy.DENSE_ONLY, score_threshold=0.1
        )
    )
    assert response.retrieved_docs
    precise = PreciseCitation(**response.retrieved_docs[0].citation.model_dump())
    assert precise.sourceType in {"STANDARD_CLAUSE", "LAW_ARTICLE", "PRECEDENT"}
    assert 0.0 <= precise.relevanceScore <= 1.0


def test_low_score_triggers_empty_recall() -> None:
    """阈值过高 → 空召回降级（缺失检测信号，不是错误）。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    service, _ = build_service(chunks)
    response = service.retrieve(
        RAGQueryRequest(
            query="乙方应缴纳社会保险",
            strategy=RetrieveStrategy.DENSE_ONLY,
            score_threshold=0.999,
        )
    )
    assert response.retrieved_docs == []
    assert response.degraded is True
    assert response.fallback_reason is FallbackReason.EMPTY_RECALL
    assert "【缺失标准对照】" in response.context_block


def test_hybrid_strategy_falls_back_to_dense_only() -> None:
    """Phase 1 无 BM25 时，HYBRID_RRF 自动退化为 DENSE_ONLY 并记录 warning。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    service, _ = build_service(chunks)
    response = service.retrieve(
        RAGQueryRequest(query="合同总价", strategy=RetrieveStrategy.HYBRID_RRF, score_threshold=0.1)
    )
    assert response.strategy is RetrieveStrategy.DENSE_ONLY
    assert any("DENSE_ONLY" in w for w in response.warnings)


def test_store_failure_degrades_to_store_unavailable() -> None:
    """向量库异常 → STORE_UNAVAILABLE 降级，绝不向上抛。"""

    class _BrokenStore:
        def ensure_collection(self, dim: int) -> None: ...
        def upsert(self, records: Any) -> int:
            return 0

        def search(self, vector: Any, top_k: int, filters: Any = None) -> list[Any]:
            raise RuntimeError("qdrant down")

        def delete_by_doc(self, doc_id: str) -> int:
            return 0

        def health(self) -> bool:
            return False

    service = RAGService(
        embedder=HashEmbedder(dim=64),
        vector_store=_BrokenStore(),  # type: ignore[arg-type]
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
    )
    response = service.retrieve(RAGQueryRequest(query="任意查询"))
    assert response.degraded is True
    assert response.fallback_reason is FallbackReason.STORE_UNAVAILABLE
    assert response.retrieved_docs == []


def test_context_budget_truncates() -> None:
    """Token 预算不足时必须截断并置 truncated=True。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(
        make_doc(
            text="\n".join(f"第{i}条 条款\n" + "内容" * 60 for i in range(1, 6))
        )
    )
    service, _ = build_service(chunks)
    response = service.retrieve(
        RAGQueryRequest(
            query="内容", top_k=5, strategy=RetrieveStrategy.DENSE_ONLY,
            score_threshold=0.0, max_context_tokens=256,
        )
    )
    assert response.retrieved_docs
    assert response.truncated is True
    assert response.context_tokens <= 256 + 50  # 允许单条溢出（至少注入最相关的一条）


def test_context_builder_empty_returns_hint() -> None:
    """无召回时上下文为固定兜底提示语。"""
    result = ContextBuilder(counter=TokenCounter(force_estimate=True)).build([], 1000)
    assert "【缺失标准对照】" in result.context_block
    assert result.injected_chunks == 0
    assert result.truncated is False


# ===========================================================================
# 5. 配置与工厂
# ===========================================================================
def test_settings_defaults_are_offline() -> None:
    """默认值必须可离线跑通：inmemory + hash + noop。"""
    cfg = RagSettings(_env_file=None)
    assert cfg.vector_store == "inmemory"
    assert cfg.embed_mode == "hash"
    assert cfg.rerank_mode == "noop"
    assert cfg.vector_dim == 1024
    assert cfg.score_threshold == 0.35
    assert cfg.strategy is RetrieveStrategy.DENSE_ONLY


def test_env_prefix_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CG_RAG_` 前缀环境变量必须能覆盖默认值。"""
    monkeypatch.setenv("CG_RAG_VECTOR_STORE", "qdrant")
    monkeypatch.setenv("CG_RAG_EMBED_MODE", "http")
    monkeypatch.setenv("CG_RAG_EMBED_MODEL", "BAAI/bge-m3")
    monkeypatch.setenv("CG_RAG_RERANK_MODE", "noop")
    cfg = RagSettings(_env_file=None)
    assert cfg.vector_store == "qdrant"
    assert cfg.embed_mode == "http"
    assert cfg.embed_model == "BAAI/bge-m3"


def test_factory_builds_offline_stack() -> None:
    """工厂按配置产出对应实现（默认离线栈）。"""
    cfg = RagSettings(_env_file=None)
    assert isinstance(build_embedder(cfg), HashEmbedder)
    assert isinstance(build_vector_store(cfg), InMemoryVectorStore)
    assert isinstance(build_reranker(cfg), NoopReranker)

    service = build_service_from_settings(cfg)
    assert isinstance(service.embedder, HashEmbedder)
    assert isinstance(service.vector_store, InMemoryVectorStore)


def test_settings_rejects_unknown_mode() -> None:
    """非法取值在**配置层**即被拒绝（Literal 约束），不可能流到工厂。"""
    with pytest.raises(ValidationError):
        RagSettings(_env_file=None, embed_mode="magic")  # type: ignore[arg-type]


def test_factory_rejects_unknown_mode() -> None:
    """工厂自身也有守卫：绕过配置校验直接构造时必须显式报错，不能静默回退。"""
    cfg = RagSettings(_env_file=None).model_copy(update={"embed_mode": "magic"})
    with pytest.raises(ValueError):
        build_embedder(cfg)
    with pytest.raises(ValueError):
        build_vector_store(RagSettings(_env_file=None).model_copy(update={"vector_store": "x"}))


# ===========================================================================
# 6. 双实现一致性
# ===========================================================================
def test_inmemory_and_qdrant_agree_on_top_ids() -> None:
    """验收标准 3：同一 query 下 InMemory 与 Qdrant 的 Top-5 chunk_id 一致。"""
    qdrant_client = pytest.importorskip("qdrant_client")
    del qdrant_client  # 仅用于 skip

    from rag_core.adapters import QdrantVectorStore

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(
        make_doc(
            text=(
                "第一条 标的\n甲方向乙方采购服务器。\n"
                "第二条 逾期付款违约金\n买方逾期付款应按日支付千分之五违约金。\n"
                "第三条 保密\n双方对商业秘密负有保密义务。"
            )
        )
    )
    embedder = HashEmbedder(dim=1024)
    vectors = embedder.embed_batch([c.text for c in chunks])
    records = [VectorRecord(chunk=c, vector=v) for c, v in zip(chunks, vectors)]

    mem = InMemoryVectorStore()
    mem.ensure_collection(1024)
    mem.upsert(records)

    qd = QdrantVectorStore(mode="local_memory", collection="t_consistency")
    qd.ensure_collection(1024)
    assert qd.upsert(records) == len(records)
    assert qd.health() is True

    query_vec = embedder.embed_one("逾期付款的违约金")
    mem_ids = [h.chunk_id for h in mem.search(query_vec, 5)]
    qd_ids = [h.chunk_id for h in qd.search(query_vec, 5)]
    assert mem_ids == qd_ids, "两套实现排序不一致，说明 port 抽象有偏差"
    assert len(mem_ids) == 3


def test_qdrant_filters_and_delete() -> None:
    """Qdrant 适配器的过滤与删除语义必须与其他实现一致。"""
    pytest.importorskip("qdrant_client")
    from rag_core.adapters import QdrantVectorStore

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    std = chunker.chunk(make_doc(doc_id="D-STD", text="第一条 价款\n合同总价壹佰万元。"))
    law = chunker.chunk(
        make_doc(
            doc_id="D-LAW",
            source_type=SourceType.LAW_ARTICLE,
            law_code="民法典",
            contract_type=None,
            text="第五百八十五条 违约金\n当事人可以约定违约金。",
        )
    )
    embedder = HashEmbedder(dim=1024)
    all_chunks = std + law
    store = QdrantVectorStore(mode="local_memory", collection="t_filters")
    store.ensure_collection(1024)
    store.upsert(
        [
            VectorRecord(chunk=c, vector=v)
            for c, v in zip(all_chunks, embedder.embed_batch([c.text for c in all_chunks]))
        ]
    )
    vec = embedder.embed_one("违约金")
    law_only = store.search(vec, 10, RetrievalFilters(source_types=[SourceType.LAW_ARTICLE]))
    assert law_only and all(h.payload["source_type"] == "LAW_ARTICLE" for h in law_only)
    assert store.delete_by_doc("D-LAW") == 1
    assert store.count() == len(std)


def test_contract_violation_is_rejected() -> None:
    """契约校验不通过的对象不能进入向量库（构造期即失败）。"""
    with pytest.raises(ValidationError):
        ContractChunk(
            chunk_id="a" * 36,
            doc_id="D",
            source_type=SourceType.LAW_ARTICLE,
            chunk_type=ChunkType.ARTICLE,
            section_id="第五百八十五条",
            seq_in_doc=0,
            text="条文",
            text_hash=ContractChunk.compute_text_hash("条文"),
            char_count=2,
            token_count=1,
        )


def _build_hybrid_service(
    chunks: list[ContractChunk], tmp_path: Path
) -> tuple[RAGService, InMemoryVectorStore]:
    """组装带 BM25 稀疏路的服务（混合策略测试用）。"""
    from rag_core.adapters.bm25_store import BM25Store

    embedder = HashEmbedder(dim=64, model_name="hash-test")
    store = InMemoryVectorStore()
    store.ensure_collection(embedder.dim)
    store.upsert(
        [
            VectorRecord(chunk=c, vector=v)
            for c, v in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
        ]
    )
    sparse = BM25Store(data_dir=tmp_path / "bm25")
    sparse.upsert(chunks)
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
        sparse_store=sparse,
        dense_top=20,
        sparse_top=20,
        rrf_k=60,
    )
    return service, store


def test_hybrid_strategy_fuses_sparse_and_dense(tmp_path: Path) -> None:
    """T03-4：HYBRID_RRF 下 fused 非 None，stage_latency_ms 含 sparse/fusion。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(
        make_doc(
            text=(
                "第一条 标的\n甲方向乙方采购服务器设备。\n"
                "第二条 逾期付款违约金\n买方逾期付款的，应按日支付合同总价千分之五的违约金。\n"
                "第三条 保密\n双方对商业秘密负有保密义务。"
            )
        )
    )
    service, _ = _build_hybrid_service(chunks, tmp_path)
    response = service.retrieve(
        RAGQueryRequest(
            query="买方逾期付款的违约金",
            strategy=RetrieveStrategy.HYBRID_RRF,
            score_threshold=0.25,
        )
    )
    assert response.retrieved_docs, "应当有召回"
    assert response.retrieved_docs[0].scores.fused is not None
    assert "sparse" in response.stage_latency_ms
    assert "fusion" in response.stage_latency_ms
    assert not any("未接入 BM25" in w for w in response.warnings)


def test_sparse_failure_does_not_break_retrieval(tmp_path: Path) -> None:
    """T03-4：BM25 抛异常时整体不降级，仍返回稠密结果（稀疏路失败隔离）。"""

    class _BrokenSparse:
        def upsert(self, chunks: Any) -> int:
            return 0

        def search(self, query: str, top_k: int) -> list[Any]:
            raise RuntimeError("bm25 down")

        def delete_by_doc(self, doc_id: str) -> int:
            return 0

        def health(self) -> bool:
            return False

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    embedder = HashEmbedder(dim=64, model_name="hash-test")
    store = InMemoryVectorStore()
    store.ensure_collection(embedder.dim)
    store.upsert(
        [
            VectorRecord(chunk=c, vector=v)
            for c, v in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
        ]
    )
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
        sparse_store=_BrokenSparse(),  # type: ignore[arg-type]
        dense_top=20,
        sparse_top=20,
    )
    response = service.retrieve(
        RAGQueryRequest(
            query="合同总价",
            strategy=RetrieveStrategy.HYBRID_RRF,
            score_threshold=0.1,
        )
    )
    assert response.degraded is False
    assert response.retrieved_docs, "稀疏路失败仍应返回稠密结果"
    assert any("BM25 检索失败" in w for w in response.warnings)


def test_rerank_failure_falls_back_to_base_scores() -> None:
    """§11.5 #3：重排抛 RerankFailedError → 回退 base_scores、置 RERANK_FAILED、不抛异常。"""
    from rag_contracts.errors import RerankFailedError

    class _BrokenReranker:
        @property
        def model_name(self) -> str:
            return "broken"

        def rerank(self, query: str, texts: Any, base_scores: Any = None) -> list[float]:
            raise RerankFailedError("rerank down")

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    embedder = HashEmbedder(dim=64, model_name="hash-test")
    store = InMemoryVectorStore()
    store.ensure_collection(embedder.dim)
    store.upsert(
        [
            VectorRecord(chunk=c, vector=v)
            for c, v in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
        ]
    )
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=TokenCounter(force_estimate=True),
        reranker=_BrokenReranker(),  # type: ignore[arg-type]
    )
    response = service.retrieve(RAGQueryRequest(query="合同总价", score_threshold=0.1))

    assert response.retrieved_docs, "重排失败仍应返回 base_scores 结果"
    assert response.degraded is True
    assert response.fallback_reason is FallbackReason.RERANK_FAILED
    assert any("重排失败" in w for w in response.warnings)
    assert all(0.0 <= d.scores.final <= 1.0 for d in response.retrieved_docs)
    assert response.retrieved_docs[0].scores.rerank is None, "未走重排，rerank 分应为 None"


# ===========================================================================
# 7. T-A0-2：稀疏路独占候选由 payload 还原，不再被丢弃
# ===========================================================================
def _build_sparse_only_service(
    chunks: list[ContractChunk], tmp_path: Path, sparse_store: Any = None
) -> RAGService:
    """稠密库为空、仅稀疏路有命中的服务（验证稀疏独占候选的还原）。"""
    from rag_core.adapters.bm25_store import BM25Store

    embedder = HashEmbedder(dim=64, model_name="hash-test")
    empty_store = InMemoryVectorStore()
    empty_store.ensure_collection(embedder.dim)  # 故意不 upsert：稠密路零候选
    if sparse_store is None:
        sparse_store = BM25Store(data_dir=tmp_path / "bm25")
        sparse_store.upsert(chunks)
    return RAGService(
        embedder=embedder,
        vector_store=empty_store,
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
        sparse_store=sparse_store,
        dense_top=20,
        sparse_top=20,
        rrf_k=60,
    )


def test_hybrid_keeps_sparse_only_candidate(tmp_path: Path) -> None:
    """T-A0-2 断言 6/7：稀疏独占候选由 payload 还原，且不再出现"无法取回"告警。"""
    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(
        make_doc(
            text=(
                "第一条 逾期付款\n"
                "买方逾期付款的，应按日支付合同总价千分之五的违约金。"
            )
        )
    )
    service = _build_sparse_only_service(chunks, tmp_path)

    response = service.retrieve(
        RAGQueryRequest(
            query="逾期付款违约金",
            strategy=RetrieveStrategy.HYBRID_RRF,
            score_threshold=0.0,
        )
    )
    assert not any("无法取回完整切片" in w for w in response.warnings)
    assert [d.chunk_id for d in response.retrieved_docs] == [c.chunk_id for c in chunks]
    # 稠密路未命中 → 稠密分 0.0（阈值判定用稠密分，故需精排或调参才能过 0.35）
    assert all(d.scores.dense == 0.0 for d in response.retrieved_docs)
    assert all(d.scores.fused is not None for d in response.retrieved_docs)


def test_hybrid_sparse_only_without_payload_still_warns(tmp_path: Path) -> None:
    """payload 缺失（旧格式索引）时仍保守丢弃并告警 —— 不把无法还原的候选当命中。"""

    class _LegacySparse:
        """模拟旧索引：命中不带 payload。"""

        def upsert(self, chunks: Any) -> int:
            return 0

        def search(self, query: str, top_k: int) -> list[SparseHit]:
            return [SparseHit(chunk_id="c-legacy", score=1.0, text="旧索引命中")]

        def delete_by_doc(self, doc_id: str) -> int:
            return 0

        def health(self) -> bool:
            return True

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True))
    chunks = chunker.chunk(make_doc())
    service = _build_sparse_only_service(
        chunks, tmp_path, sparse_store=_LegacySparse()  # type: ignore[arg-type]
    )
    response = service.retrieve(
        RAGQueryRequest(
            query="逾期付款违约金",
            strategy=RetrieveStrategy.HYBRID_RRF,
            score_threshold=0.0,
        )
    )
    assert any("无法取回完整切片" in w for w in response.warnings)
    assert response.retrieved_docs == []
    assert response.degraded is True
