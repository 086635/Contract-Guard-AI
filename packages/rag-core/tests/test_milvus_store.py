"""Milvus 备选适配器自检（T-A1 · 组件卡 `1.C5` §⑧）。

**离线部分（默认运行，不依赖 Milvus 服务）**：
1. 协议自证与 `name`（注入假 `pymilvus`，不建连）；
2. 顶层无 `pymilvus` import（AST 断言）+ 缺包时包出口仍可导入（PEP 562 懒导出）；
3. 默认值红线：`RagSettings().vector_store` 仍是 `inmemory`；
4. 纯函数：`_escape` / `_build_expr` / `_row_of` / `_score_from_distance` / 列宽；
5. 与 Qdrant 侧 6 个标量列字段名不得漂移。

**集成部分（`CG_RAG_TEST_MILVUS_URI` 未设置时 skip，保持离线基线）** ——
逐条对应 `1-5集成测试与迁移.md` §⑤ 的 7 条断言：

| # | 断言 | 用例 |
|---|---|---|
| 1 | `ensure_collection(dim)` 连调 2 次不抛异常（幂等） | `test_ensure_collection_idempotent` |
| 2 | 同一 `chunk_id` `upsert` 两次 → `count()` 不增长 | `test_upsert_idempotent` |
| 3 | `search` 的 payload 能 `ContractChunk.from_payload()` 还原且等值 | `test_search_score_is_cosine_similarity` |
| 4 | 过滤命中；不带过滤时能召回到不同源的其它切片 | `test_search_respects_filters` |
| 5 | 过滤值含 `"` 时 expr 不报错且结果正确 | `test_search_handles_quoted_filter_value` |
| 6 | `delete_by_doc()` 后 `count()` 减少且 `search` 不再返回该 doc | `test_delete_by_doc_reduces_count_and_hides_hits` |
| 7 | 与 `InMemoryVectorStore` 的 Top-10 `chunk_id` 重合度 ≥ 0.8 | `test_milvus_top10_overlap` |

⚠️ 集成用例用**逐用例** `skipif`，**不用**文件级 `pytestmark` —— 否则离线用例会被一并
跳过，离线基线表面不退化、实际从未运行（`1-5集成测试与迁移.md` §⑤ 避坑 1）。
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from rag_contracts.chunk import EMBEDDING_DIM, ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType
from rag_contracts.errors import StoreUnavailableError
from rag_contracts.ports import VectorRecord, VectorStorePort
from rag_contracts.query import RetrievalFilters
from rag_core.adapters import HashEmbedder, InMemoryVectorStore
from rag_core.adapters.milvus_store import (
    _MAX_LEN,
    _PK_FIELD,
    _SCALAR_FIELDS,
    MilvusVectorStore,
)
from rag_core.adapters.qdrant_store import KEYWORD_INDEX_FIELDS
from rag_core.config import RagSettings
from rag_core.factory import build_vector_store

_MODULE_PATH = Path(
    importlib.import_module("rag_core.adapters.milvus_store").__file__ or ""
)


# ===========================================================================
# 假 pymilvus（离线：不建连、不装包也能跑）
# ===========================================================================
class _FakeSchema:
    """假 CollectionSchema：只记录 add_field 调用。"""

    def __init__(self) -> None:
        self.fields: list[tuple[str, str, dict[str, Any]]] = []

    def add_field(self, name: str, dtype: str, **kwargs: Any) -> None:
        self.fields.append((name, dtype, kwargs))


class _FakeIndexParams:
    """假 IndexParams：只记录 add_index 调用。"""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def add_index(self, **kwargs: Any) -> None:
        self.entries.append(kwargs)


class _FakeClient:
    """假 MilvusClient：记录构造参数 + 建集合路径，不发任何网络请求。"""

    def __init__(self, *, exists: bool = False) -> None:
        self.kwargs: dict[str, Any] = {}
        self.exists = exists
        self.schema = _FakeSchema()
        self.index_params = _FakeIndexParams()
        self.created: tuple[str, Any, Any] | None = None
        self.loaded: list[str] = []

    # 构造参数捕获
    def capture(self, **kwargs: Any) -> _FakeClient:
        self.kwargs = kwargs
        return self

    # MilvusClient 的最小 API 面
    def has_collection(self, name: str) -> bool:
        return self.exists

    def load_collection(self, name: str) -> None:
        self.loaded.append(name)

    def create_schema(self, **kwargs: Any) -> _FakeSchema:
        return self.schema

    def prepare_index_params(self) -> _FakeIndexParams:
        return self.index_params

    def create_collection(self, name: str, schema: Any, index_params: Any) -> None:
        self.created = (name, schema, index_params)


@pytest.fixture()
def fake_pymilvus(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """注入假 `pymilvus` 模块，使 `MilvusVectorStore()` 可离线构造。"""
    holder: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> _FakeClient:
        client = _FakeClient()
        holder["client"] = client.capture(**kwargs)
        return holder["client"]

    module = types.ModuleType("pymilvus")
    module.MilvusClient = _make_client  # type: ignore[attr-defined]
    module.DataType = types.SimpleNamespace(  # type: ignore[attr-defined]
        VARCHAR="VARCHAR", FLOAT_VECTOR="FLOAT_VECTOR", JSON="JSON"
    )
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return holder


# ===========================================================================
# 1. 协议自证 / 标识 / 连接参数
# ===========================================================================
def test_implements_vector_store_port(fake_pymilvus: dict[str, Any]) -> None:
    """§⑧ 用例 1：结构性满足 `VectorStorePort`（6 个方法名齐备）。"""
    store = MilvusVectorStore()
    assert isinstance(store, VectorStorePort)
    del store
    assert fake_pymilvus["client"].kwargs["uri"] == "http://localhost:19530"


def test_name_reflects_mode(fake_pymilvus: dict[str, Any]) -> None:
    """断言 4：`name` 形如 `milvus:server` / `milvus:lite`。"""
    assert MilvusVectorStore().name == "milvus:server"
    assert MilvusVectorStore(mode="lite", uri="./m.db").name == "milvus:lite"


def test_build_client_forwards_connection_params(fake_pymilvus: dict[str, Any]) -> None:
    """连接参数（含 timeouts）真的透传给 SDK；空 token 不传该 kwarg。"""
    MilvusVectorStore(
        uri="http://milvus:19530", db_name="rag", collection="c2", timeout_s=7.5
    )
    kwargs = fake_pymilvus["client"].kwargs
    assert kwargs == {"uri": "http://milvus:19530", "db_name": "rag", "timeout": 7.5}

    MilvusVectorStore(uri="http://milvus:19530", token="u:p")
    assert fake_pymilvus["client"].kwargs["token"] == "u:p"


def test_invalid_mode_raises(fake_pymilvus: dict[str, Any]) -> None:
    """断言 5：非法 `mode` 抛 `StoreUnavailableError`。"""
    with pytest.raises(StoreUnavailableError):
        MilvusVectorStore(mode="bogus")


# ===========================================================================
# 2. 延迟导入（离线红线）
# ===========================================================================
def test_no_top_level_pymilvus_import() -> None:
    """§⑧ 用例 2：模块顶层不得 import `pymilvus`（源码 AST 断言）。"""
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    assert not [m for m in top_level if m.split(".")[0] == "pymilvus"]


def test_package_export_works_without_pymilvus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """断言 1/2：缺 `pymilvus` 时包出口仍能导入，且构造失败是 `StoreUnavailableError`。"""
    monkeypatch.setitem(sys.modules, "pymilvus", None)  # import pymilvus → ImportError
    adapters = importlib.import_module("rag_core.adapters")
    exported = getattr(adapters, "MilvusVectorStore")  # 触发 PEP 562 懒导出
    assert exported is MilvusVectorStore
    with pytest.raises(StoreUnavailableError):
        exported()


# ===========================================================================
# 3. 默认值红线
# ===========================================================================
def test_not_used_as_default() -> None:
    """§⑧ 用例 3：Milvus 不得成为任何默认值（决策 D1）。"""
    settings = RagSettings()
    assert settings.vector_store == "inmemory"
    assert isinstance(build_vector_store(settings), InMemoryVectorStore)


# ===========================================================================
# 4. 纯函数：转义 / expr / 摊平 / 分数口径 / 列宽
# ===========================================================================
def test_escape_escapes_backslash_and_quote() -> None:
    """`_escape`（静态方法，离线可跑）：反斜杠与双引号都转义。"""
    assert MilvusVectorStore._escape('a"b') == 'a\\"b'
    assert MilvusVectorStore._escape("C:\\x") == "C:\\\\x"


def test_build_expr_empty_cases() -> None:
    """无过滤条件 → 空字符串（不得产出 `and` 悬空子句）。"""
    assert MilvusVectorStore._build_expr(None) == ""
    assert MilvusVectorStore._build_expr(RetrievalFilters()) == ""


def test_build_expr_covers_all_dimensions() -> None:
    """6 个过滤维度 → 与 `qdrant_store._build_filter` 语义等价的 expr。"""
    filters = RetrievalFilters(
        source_types=[SourceType.LAW_ARTICLE, SourceType.STANDARD_CLAUSE],
        chunk_types=[ChunkType.ARTICLE],
        contract_type=ContractType.PURCHASE,
        law_codes=["民法典"],
        doc_ids=["DOC-1"],
        categories=["PAYMENT_TERM_RISK"],
    )
    assert MilvusVectorStore._build_expr(filters) == (
        'source_type in ["LAW_ARTICLE", "STANDARD_CLAUSE"]'
        ' and chunk_type in ["ARTICLE"]'
        ' and contract_type == "PURCHASE"'
        ' and law_code in ["民法典"]'
        ' and doc_id in ["DOC-1"]'
        ' and category in ["PAYMENT_TERM_RISK"]'
    )


def test_build_expr_escapes_quoted_values() -> None:
    """值含 `"` / `\\` 时不产生语法错（转义生效）。"""
    filters = RetrievalFilters(categories=['X"Y', "C:\\tmp"])
    assert MilvusVectorStore._build_expr(filters) == (
        'category in ["X\\"Y", "C:\\\\tmp"]'
    )


def _sample_chunk(**overrides: Any) -> ContractChunk:
    """构造一条合法切片（法条：带 law_code、无 contract_type）。"""
    text = overrides.pop("text", "第五百八十五条 违约金")
    payload: dict[str, Any] = {
        "chunk_id": "MILVUS-UNIT-0001",
        "doc_id": "民法典",
        "source_type": SourceType.LAW_ARTICLE,
        "chunk_type": ChunkType.ARTICLE,
        "section_id": "第五百八十五条",
        "seq_in_doc": 0,
        "text": text,
        "text_hash": ContractChunk.compute_text_hash(text),
        "char_count": len(text),
        "token_count": 8,
        "law_code": "民法典",
        "category": "PAYMENT_TERM_RISK",
    }
    payload.update(overrides)
    return ContractChunk(**payload)


def test_row_of_flattens_chunk_and_maps_none_to_empty() -> None:
    """`_row_of`：JSON payload 保真 + 6 个标量列摊平，None → ""。"""
    chunk = _sample_chunk(contract_type=None)
    row = MilvusVectorStore._row_of(VectorRecord(chunk=chunk, vector=[0.1, 0.2]))

    assert row[_PK_FIELD] == chunk.chunk_id
    assert row["vector"] == [0.1, 0.2]
    assert row["payload"] == chunk.to_payload()
    assert set(row) == {_PK_FIELD, "vector", "payload", *_SCALAR_FIELDS}
    assert row["contract_type"] == ""  # None → ""（Milvus 标量列无 null）
    assert row["source_type"] == "LAW_ARTICLE"  # 枚举取 .value
    assert row["law_code"] == "民法典"


def test_score_from_distance_is_similarity() -> None:
    """分数口径：两个后端的 COSINE `distance` 语义**相反**，必须按 `mode` 分派。

    实测（2026-09-24，pymilvus 3.0.0，同一对向量 `cos(v0,v1) = 0.0586883`）：
      - `milvus-lite 3.0`：自检索 `distance = 0.0`、`cos=0.0587` 时 `distance = 0.9413117`
        —— 语义是**余弦距离** `1 - cos`
      - `milvusdb/milvus v2.5.4`：自检索 `distance = 1.0`、`cos=0.0587` 时 `distance = 0.0586883`
        —— 语义是**余弦相似度** `cos`

    契约 `VectorHit.score` 要求「越大越相似」，故 server 取原值、lite 取 `1 - distance`。
    """
    client = _FakeClient()

    server = _store_with(client)
    server._mode = "server"
    assert server._score_from_distance(1.0) == pytest.approx(1.0)
    assert server._score_from_distance(0.0586883) == pytest.approx(0.0586883)

    lite = _store_with(client)
    lite._mode = "lite"
    assert lite._score_from_distance(0.0) == pytest.approx(1.0)
    assert lite._score_from_distance(0.9413117) == pytest.approx(0.0586883)


def test_scalar_fields_match_qdrant_keyword_fields() -> None:
    """守卫：两库的过滤字段名不得漂移（否则同一 filters 过滤出不同结果）。"""
    assert _SCALAR_FIELDS == KEYWORD_INDEX_FIELDS


def test_max_len_covers_contract_limits() -> None:
    """守卫：VARCHAR 列宽必须覆盖 `ContractChunk` 的 max_length（否则写入被拒）。"""
    for field in (_PK_FIELD, "doc_id", "law_code", "category"):
        field_info = ContractChunk.model_fields[field]
        limit = next(
            (
                getattr(meta, "max_length", None)
                for meta in field_info.metadata
                if isinstance(getattr(meta, "max_length", None), int)
            ),
            None,
        )
        assert limit is not None, f"{field} 未声明 max_length，本守卫需同步"
        assert _MAX_LEN[field] >= limit


# ===========================================================================
# 5. ensure_collection：幂等 + 不建标量索引（版本适配）+ 已存在则 load
# ===========================================================================
def _store_with(client: _FakeClient) -> MilvusVectorStore:
    """用假 client 装配一个适配器实例（绕过 __init__ 的建连）。"""
    store = object.__new__(MilvusVectorStore)
    store._mode = "server"
    store._collection = "test_contract_clauses"
    store.name = "milvus:server"
    store._client = client
    return store


def test_ensure_collection_builds_schema_without_scalar_indexes(
    fake_pymilvus: dict[str, Any],
) -> None:
    """新建路径：schema 含主键/向量/JSON/6 标量列；索引只建向量（不建 INVERTED）。

    ⚠️ 实测 pymilvus 3.0.0 + milvus-lite 3.0 不接受标量 INVERTED 索引
    （translator 强制 metric_type、engine 只接受向量字段）→ 按 `1-2 §⑦-5` 去掉该循环。
    """
    client = _FakeClient(exists=False)
    _store_with(client).ensure_collection(dim=EMBEDDING_DIM)

    assert client.created is not None and client.created[0] == "test_contract_clauses"
    field_names = [name for name, _, _ in client.schema.fields]
    assert field_names == [_PK_FIELD, "vector", "payload", *_SCALAR_FIELDS]
    assert client.schema.fields[0][2]["is_primary"] is True
    assert client.schema.fields[0][2]["max_length"] == _MAX_LEN[_PK_FIELD]

    assert [entry["index_type"] for entry in client.index_params.entries] == ["HNSW"]
    assert client.index_params.entries[0]["metric_type"] == "COSINE"
    assert client.loaded == []


def test_ensure_collection_loads_existing_collection(
    fake_pymilvus: dict[str, Any],
) -> None:
    """已存在路径：不重复建集合，但必须 load（否则 released 状态下 search 报 101）。"""
    client = _FakeClient(exists=True)
    _store_with(client).ensure_collection(dim=EMBEDDING_DIM)

    assert client.created is None
    assert client.loaded == ["test_contract_clauses"]


# ===========================================================================
# 6. 集成用例（未设 CG_RAG_TEST_MILVUS_URI 时 skip）
# ===========================================================================
_MILVUS_URI = os.getenv("CG_RAG_TEST_MILVUS_URI")
_requires_milvus = pytest.mark.skipif(
    not _MILVUS_URI,
    reason="未设置 CG_RAG_TEST_MILVUS_URI，跳过 Milvus 集成测试（保持离线基线）",
)

_IT_COLLECTION = "test_contract_clauses"
_IT_STD_DOC = "MILVUS-IT-STD"
_IT_LAW_DOC = "MILVUS-IT-LAW"
# 断言 5 专用：doc_id **故意含双引号**，用于验证 `_build_expr` 的 `_escape` 在真机 expr 上生效
_IT_QUOTE_DOC = 'MILVUS-IT-QUOTE"D'
# 集成用例会写到的全部 doc_id —— fixture 前后各清一次，保证用例互不污染
_IT_DOCS = (_IT_STD_DOC, _IT_LAW_DOC, _IT_QUOTE_DOC)
# ⚠️ 语料必须**互异**：若各切片措辞雷同，HashEmbedder（伪向量）会产出大量同分候选，
# 同分顺序由实现自行决定 → 重合度会因「排序抖动」而非「语义不一致」掉下来
# （实测：100 条雷同语料重合度仅 0.30，24 条互异语料为 1.00）。见 `1.C5` §⑦ 回填表。
_IT_TEXTS = (
    "甲方向乙方采购设备，交付地点为北京市海淀区。",
    "逾期付款的，每逾期一日按未付款项的万分之五支付违约金。",
    "因不可抗力致使合同无法履行的，双方互不承担违约责任。",
    "保密义务在合同终止后继续有效三年。",
    "合同争议提交北京仲裁委员会仲裁解决。",
    "乙方保证所供货物不存在权利瑕疵与知识产权纠纷。",
    "质保期为验收合格之日起十二个月。",
    "甲方有权在提前三十日书面通知后解除本合同。",
    "发票类型为增值税专用发票，税率百分之十三。",
    "任何一方不得未经对方书面同意转让本合同项下权利义务。",
    "保险费用由乙方承担，保额不低于合同总价的百分之十。",
    "验收标准以双方确认的技术协议为准。",
    "第五百八十五条 当事人可以约定一方违约时支付违约金。",
    "第五百六十三条 当事人一方迟延履行主要债务，经催告后在合理期限内仍未履行的，可以解除合同。",
    "第五百零九条 当事人应当按照约定全面履行自己的义务。",
    "第五百七十七条 当事人一方不履行合同义务的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
    "第五百九十条 当事人一方因不可抗力不能履行合同的，应当及时通知对方。",
    "第六百二十六条 买受人应当按照约定的数额和支付方式支付价款。",
    "第六百二十七条 买受人应当按照约定的地点支付价款。",
    "第六百二十九条 出卖人交付的标的物应当符合约定的质量标准。",
    "第五百六十六条 合同解除后，尚未履行的终止履行。",
    "第五百八十四条 损失赔偿额应当相当于因违约造成的损失。",
    "第五百八十六条 当事人可以约定一方向对方给付定金作为债权的担保。",
    "第六百一十七条 出卖人交付的标的物不符合质量要求的，买受人可以请求减少价款。",
)
# 前 12 条为标准条款（`第X条` 之外的行），其后为法条
_IT_LAW_FROM = 12


def _corpus() -> list[ContractChunk]:
    """确定性语料：标准条款 + 法条各若干条（同一 Embedder 生成向量）。"""
    chunks: list[ContractChunk] = []
    for idx, text in enumerate(_IT_TEXTS):
        is_law = idx >= _IT_LAW_FROM
        payload: dict[str, Any] = {
            "chunk_id": f"MILVUS-IT-{idx:04d}",
            "doc_id": _IT_LAW_DOC if is_law else _IT_STD_DOC,
            "source_type": SourceType.LAW_ARTICLE
            if is_law
            else SourceType.STANDARD_CLAUSE,
            "chunk_type": ChunkType.ARTICLE if is_law else ChunkType.CLAUSE,
            "section_id": text.split(" ")[0],
            "seq_in_doc": idx,
            "text": text,
            "text_hash": ContractChunk.compute_text_hash(text),
            "char_count": len(text),
            "token_count": max(1, len(text) // 2),
            "category": "PAYMENT_TERM_RISK",
            "contract_type": None if is_law else ContractType.PURCHASE,
        }
        if is_law:
            payload["law_code"] = "民法典"
        chunks.append(ContractChunk(**payload))
    return chunks


@pytest.fixture()
def milvus_store() -> Any:
    """连真 Milvus（URI 来自 `CG_RAG_TEST_MILVUS_URI`），用独立 collection 并清理。"""
    store = MilvusVectorStore(uri=_MILVUS_URI or "", collection=_IT_COLLECTION)
    store.ensure_collection(EMBEDDING_DIM)
    for doc_id in _IT_DOCS:
        store.delete_by_doc(doc_id)
    yield store
    for doc_id in _IT_DOCS:
        store.delete_by_doc(doc_id)


@_requires_milvus
def test_ensure_collection_idempotent(milvus_store: MilvusVectorStore) -> None:
    """§⑤ 断言 1：`ensure_collection(dim)` 连调 2 次不抛异常（幂等）。

    fixture 已建过一次（走 `create_collection` 分支）；此处再连调 2 次，覆盖
    「已存在 → `load_collection` → 返回」分支 —— 实测跨进程集合可能停在 `released`，
    `load` 必须幂等（`1-2 §⑪.3-#4`）。幂等重建后集合仍须可用：写 1 条能原样检索到。
    """
    milvus_store.ensure_collection(EMBEDDING_DIM)
    milvus_store.ensure_collection(EMBEDDING_DIM)

    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunk = _corpus()[0]
    milvus_store.upsert(
        [VectorRecord(chunk=chunk, vector=embedder.embed_one(chunk.text))]
    )
    hits = milvus_store.search(embedder.embed_one(chunk.text), 1)
    assert hits and hits[0].chunk_id == chunk.chunk_id


@_requires_milvus
def test_milvus_top10_overlap(milvus_store: MilvusVectorStore) -> None:
    """§⑧ 用例 5：同一语料 + 同一 Embedder，两库 Top-10 重合度 ≥ 0.8。"""
    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunks = _corpus()
    records = [
        VectorRecord(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    inmemory = InMemoryVectorStore()
    inmemory.ensure_collection(EMBEDDING_DIM)

    assert milvus_store.upsert(records) == len(records)
    inmemory.upsert(records)

    query = embedder.embed_one("违约金 上限 付款 期限")
    reference = [hit.chunk_id for hit in inmemory.search(query, 10)]
    candidate = [hit.chunk_id for hit in milvus_store.search(query, 10)]
    overlap = len(set(reference) & set(candidate)) / max(1, len(set(reference)))
    assert overlap >= 0.8, (
        f"两库 Top-10 重合度过低：{overlap:.2f}；"
        f"仅 Milvus 命中={sorted(set(candidate) - set(reference))}；"
        f"仅 InMemory 命中={sorted(set(reference) - set(candidate))}"
    )


@_requires_milvus
def test_upsert_idempotent(milvus_store: MilvusVectorStore) -> None:
    """§⑧ 用例 6：同一 `chunk_id` 重复写入 → `count()` 不增长。"""
    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunks = _corpus()
    records = [
        VectorRecord(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    before = milvus_store.count()
    assert milvus_store.upsert(records) == len(records)
    after_first = milvus_store.count()
    milvus_store.upsert(records)
    assert milvus_store.count() == after_first == before + len(records)


@_requires_milvus
def test_search_score_is_cosine_similarity(milvus_store: MilvusVectorStore) -> None:
    """`score` 口径守卫：自检索必须≈1.0（若按 Milvus 原始 distance 取值则为 0.0）。"""
    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunks = _corpus()
    records = [
        VectorRecord(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    milvus_store.upsert(records)

    target = records[0]
    hits = milvus_store.search(list(target.vector), 3)
    assert hits and hits[0].chunk_id == target.chunk.chunk_id
    assert hits[0].score == pytest.approx(1.0, abs=1e-3)
    assert hits[0].payload == target.chunk.to_payload()
    # 断言 6：search 的 payload 必须能被 ContractChunk.from_payload() 零改动还原
    assert ContractChunk.from_payload(hits[0].payload) == target.chunk


@_requires_milvus
def test_search_respects_filters(milvus_store: MilvusVectorStore) -> None:
    """过滤语义与 Qdrant 分支对齐：source_type / doc_id / law_code 三路各验一次。"""
    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunks = _corpus()
    records = [
        VectorRecord(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    milvus_store.upsert(records)

    query = embedder.embed_one("违约金 上限 付款 期限")
    law_only = milvus_store.search(
        query, 10, RetrievalFilters(source_types=[SourceType.LAW_ARTICLE])
    )
    assert law_only
    assert all(hit.payload["source_type"] == "LAW_ARTICLE" for hit in law_only)
    assert milvus_store.search(query, 10, RetrievalFilters(doc_ids=[_IT_STD_DOC]))
    assert milvus_store.search(query, 10, RetrievalFilters(law_codes=["民法典"]))
    assert (
        milvus_store.search(query, 10, RetrievalFilters(doc_ids=["NO-SUCH-DOC"])) == []
    )
    # 断言 4 后半：**不带过滤**时应能召回到不同源的其它切片（过滤确实在起作用，
    # 而不是「整个集合本来就只有一种源」）
    unfiltered = milvus_store.search(query, 10)
    assert {hit.payload["source_type"] for hit in unfiltered} == {
        SourceType.STANDARD_CLAUSE.value,
        SourceType.LAW_ARTICLE.value,
    }, "不带过滤时应同时召回标准条款与法条"


@_requires_milvus
def test_search_handles_quoted_filter_value(milvus_store: MilvusVectorStore) -> None:
    """§⑤ 断言 5：过滤值含 `"` 时不报错，且结果正确（`_escape` 在真机 expr 上生效）。

    语料里另有 24 条正常切片：若 `_escape` 缺失，expr 在真机上会直接语法错；
    若转义后语义错位，过滤结果会多出或漏掉切片 —— 两种错这里都能抓到。
    """
    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunks = _corpus()
    quoted = ContractChunk.from_payload(
        {
            **chunks[0].to_payload(),
            "chunk_id": "MILVUS-IT-QUOTE-0000",
            "doc_id": _IT_QUOTE_DOC,
        }
    )
    records = [
        VectorRecord(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    records.append(VectorRecord(chunk=quoted, vector=embedder.embed_one(quoted.text)))
    milvus_store.upsert(records)

    hits = milvus_store.search(
        embedder.embed_one(quoted.text), 10, RetrievalFilters(doc_ids=[_IT_QUOTE_DOC])
    )
    assert [hit.chunk_id for hit in hits] == [quoted.chunk_id]
    assert hits[0].payload["doc_id"] == _IT_QUOTE_DOC


@_requires_milvus
def test_delete_by_doc_reduces_count_and_hides_hits(
    milvus_store: MilvusVectorStore,
) -> None:
    """§⑤ 断言 6：`delete_by_doc()` 后 `count()` 减少，且 `search` 不再返回该 doc。

    ⚠️ 本断言在真 server 上**原本必然失败**：`count()` 旧实现读
    `get_collection_stats()["row_count"]`（物理行数、且未 flush 前恒为 0）。
    改成 `count(*)` 聚合 + `consistency_level="Strong"` 之后才成立（`1-2 §⑪.6`）。
    """
    embedder = HashEmbedder(dim=EMBEDDING_DIM)
    chunks = _corpus()
    records = [
        VectorRecord(chunk=chunk, vector=vector)
        for chunk, vector in zip(chunks, embedder.embed_batch([c.text for c in chunks]))
    ]
    milvus_store.upsert(records)
    before = milvus_store.count()
    std_count = sum(1 for chunk in chunks if chunk.doc_id == _IT_STD_DOC)
    assert std_count > 0

    removed = milvus_store.delete_by_doc(_IT_STD_DOC)

    after = milvus_store.count()
    assert after == before - std_count, (
        f"count 未按预期减少：{before} → {after}（delete_by_doc 报告的删除数={removed}）"
    )
    hits = milvus_store.search(embedder.embed_one(_IT_TEXTS[0]), 10)
    assert hits, "删掉标准条款后，法条切片仍应被召回"
    assert all(hit.payload["doc_id"] != _IT_STD_DOC for hit in hits)
