"""BM25 稀疏检索自检（设计文档 T03-1 验收标准）。

覆盖：
1. 协议实现：`isinstance(store, SparseStorePort)`
2. 序关系：命中词越多分越高（score(c0) > score(c1) > score(c2)）
3. 空库 / 空查询 / 零命中：返回空或省略无关文档
4. 幂等 upsert：同 chunk 二次写入 `stats()["docs"]` 不增长
5. 增量更新：`delete_by_doc` 回退 DF（IDF 增大，同文档分数上升）
6. pickle 持久化：save/load 往返一致；损坏索引静默降级为空

⚠️ 关于 `score(c2) == 0.0`（§8.5）：
该断言基于 RAG对照解析.md §3.5 的**纯中文二元组切词**；本项目
`tokenize` 还包含 CJK 单字，`c2`（"试用期…"）与查询因单字"期"存在
交集，故绝对值 0 分不可复用。稳定可复用的是**序关系**（§8.5 已声明
"实际数值必然不同"）。零命中语义改由 `test_zero_hit_document_is_omitted`
用"与查询完全无 token 交集"的文档验证。
"""

from __future__ import annotations

from pathlib import Path

from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, SourceType
from rag_contracts.ports import SparseStorePort
from rag_core.adapters.bm25_store import BM25Store

# 玩具语料（RAG对照解析.md §3.5）
_CORPUS = {
    "c0": "买方逾期付款的，应按日支付合同总价千分之五的违约金",
    "c1": "违约金总额累计不超过合同总价的百分之十",
    "c2": "试用期最长不得超过六个月",
}
_QUERY = "逾期付款违约金上限"


def make_chunk(doc_id: str, section_id: str, text: str) -> ContractChunk:
    """构造最小合法切片。"""
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id(doc_id, section_id),
        doc_id=doc_id,
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id=section_id,
        seq_in_doc=0,
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=0,
    )


def build_store(tmp_path: Path) -> BM25Store:
    """构造落盘到临时目录的空 BM25Store。"""
    return BM25Store(data_dir=tmp_path / "bm25")


def _score_map(store: BM25Store, query: str, top_k: int = 20) -> dict[str, float]:
    """把 search 结果转成 {chunk_id: score}，便于断言。"""
    return {h.chunk_id: h.score for h in store.search(query, top_k=top_k)}


# ===========================================================================
# 1. 协议实现
# ===========================================================================
def test_implements_sparse_store_port(tmp_path: Path) -> None:
    """BM25Store 结构上满足 SparseStorePort（runtime_checkable）。"""
    store = build_store(tmp_path)
    assert isinstance(store, SparseStorePort)
    assert store.health() is True


# ===========================================================================
# 2. 空库 / 空查询
# ===========================================================================
def test_empty_store_returns_empty(tmp_path: Path) -> None:
    """空库检索返回 []，不抛异常。"""
    store = build_store(tmp_path)
    assert store.search(_QUERY) == []


def test_empty_query_returns_empty(tmp_path: Path) -> None:
    """空查询串返回 []。"""
    store = build_store(tmp_path)
    store.upsert([make_chunk("DOC-1", "s1", "买方逾期付款的违约金")])
    assert store.search("") == []


# ===========================================================================
# 3. BM25 序关系
# ===========================================================================
def test_bm25_ordering_by_relevance(tmp_path: Path) -> None:
    """命中词越多分越高：score(c0) > score(c1) > score(c2)。"""
    store = build_store(tmp_path)
    chunks = [make_chunk("CORPUS", cid, text) for cid, text in _CORPUS.items()]
    assert store.upsert(chunks) == 3

    by_id = _score_map(store, _QUERY)
    c0, c1, c2 = (c.chunk_id for c in chunks)
    assert by_id[c0] > by_id[c1] > by_id[c2] > 0


def test_zero_hit_document_is_omitted(tmp_path: Path) -> None:
    """与查询完全无 token 交集的文档得 0 分，不出现在结果里。"""
    store = build_store(tmp_path)
    hit = make_chunk("DOC-HIT", "s1", "买方逾期付款应按日支付违约金")
    miss = make_chunk("DOC-MISS", "s2", "甲乙双方应保守商业秘密")
    store.upsert([hit, miss])

    hits = store.search(_QUERY, top_k=10)
    assert [h.chunk_id for h in hits] == [hit.chunk_id]


def test_hit_carries_text(tmp_path: Path) -> None:
    """SparseHit.text 必须回传原文（下游重排取文本）。"""
    store = build_store(tmp_path)
    chunk = make_chunk("DOC-1", "s1", "买方逾期付款的违约金")
    store.upsert([chunk])
    hits = store.search("逾期付款", top_k=10)
    assert hits and hits[0].text == chunk.text


def test_top_k_truncates(tmp_path: Path) -> None:
    """top_k 截断生效。"""
    store = build_store(tmp_path)
    store.upsert([make_chunk("CORPUS", cid, text) for cid, text in _CORPUS.items()])
    assert len(store.search(_QUERY, top_k=1)) == 1
    assert store.search(_QUERY, top_k=0) == []


# ===========================================================================
# 4. 幂等 upsert
# ===========================================================================
def test_upsert_is_idempotent(tmp_path: Path) -> None:
    """同一 chunk 二次写入：docs 数不增长，得分不变。"""
    store = build_store(tmp_path)
    chunk = make_chunk("DOC-1", "s1", "买方逾期付款的违约金")
    assert store.upsert([chunk]) == 1
    before = _score_map(store, "逾期付款")

    assert store.upsert([chunk]) == 1
    assert store.stats()["docs"] == 1
    assert _score_map(store, "逾期付款") == before


# ===========================================================================
# 5. 增量更新：delete_by_doc + DF 回退
# ===========================================================================
def test_delete_by_doc(tmp_path: Path) -> None:
    """按 doc_id 删除全部切片，被删切片不再可检索。"""
    store = build_store(tmp_path)
    keep = make_chunk("DOC-KEEP", "s1", "买方逾期付款的违约金")
    drop = make_chunk("DOC-DROP", "s2", "试用期最长不得超过六个月")
    store.upsert([keep, drop])
    assert store.stats()["docs"] == 2

    assert store.delete_by_doc("DOC-DROP") == 1
    assert store.stats()["docs"] == 1
    hits = store.search(_QUERY, top_k=10)
    assert [h.chunk_id for h in hits] == [keep.chunk_id]


def test_delete_rolls_back_df_increases_idf(tmp_path: Path) -> None:
    """删除同词文档后 DF 回退 → IDF 增大 → 剩余文档分数上升。

    用两篇等长、共享词"商业秘密"的文档隔离 avgdl 变量：
    删除一篇后 df 由 2 降为 1，IDF 增大，另一篇同 query 得分必然更高。
    """
    store = build_store(tmp_path)
    a = make_chunk("DOC-A", "s1", "甲方应保守商业秘密")
    b = make_chunk("DOC-B", "s2", "乙方应保守商业秘密")
    store.upsert([a, b])

    score_b_before = _score_map(store, "商业秘密")[b.chunk_id]
    store.delete_by_doc("DOC-A")
    score_b_after = _score_map(store, "商业秘密")[b.chunk_id]
    assert score_b_after > score_b_before


# ===========================================================================
# 6. pickle 持久化与损坏降级
# ===========================================================================
def test_persistence_roundtrip(tmp_path: Path) -> None:
    """save 后重新构造，索引规模与检索结果一致。"""
    data_dir = tmp_path / "bm25"
    store = build_store(tmp_path)
    store.upsert([make_chunk("CORPUS", cid, text) for cid, text in _CORPUS.items()])

    reloaded = BM25Store(data_dir=data_dir)
    assert reloaded.stats()["docs"] == store.stats()["docs"]
    assert [h.chunk_id for h in reloaded.search(_QUERY)] == [
        h.chunk_id for h in store.search(_QUERY)
    ]


def test_corrupted_index_falls_back_empty(tmp_path: Path) -> None:
    """索引损坏时静默降级为空索引，而非崩溃。"""
    data_dir = tmp_path / "bm25"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "index.pkl").write_bytes(b"not a valid pickle")

    store = BM25Store(data_dir=data_dir)
    assert store.stats()["docs"] == 0
    assert store.search(_QUERY) == []
    assert store.health() is True
