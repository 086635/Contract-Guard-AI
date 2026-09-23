"""T-A7 配置接线自检（`1-3 §⑧` / §⑨ 断言 8·9·10 / §⑩）。

覆盖：
1. `strategy` / `score_threshold` / `rerank_top_n`（原「声明了但无消费者」）**真正生效**，
   且**请求显式传值时请求优先**（配置只是服务级兜底）；
2. `rerank_threshold` **只在精排真正生效时**叠加（noop 路径必须保持原行为）；
3. `log_level` 的重复声明已删除、`cache_ttl_s` 已显式登记为未实现、
   `max_retries` / `max_query_chars` 已写明消费方归属（Agent 工具层 T-B4）；
4. `**overrides` 的反射命名（`_<同名>`）对新增字段同样有效。
5. `vector_store=milvus` 的接线（T-A2 / `0-C1 §⑦-1~§⑦-7`）：三分支类型、Literal 取值、
   5 个 `milvus_*` 默认值、`collection` 复用、非法取值报错。
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import Any, get_args

import pytest

from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, RetrieveStrategy, SourceType
from rag_contracts.ports import VectorRecord
from rag_contracts.query import RAGQueryRequest
from rag_core import factory as factory_module
from rag_core.config import RagSettings
from rag_core.factory import build_service_from_settings, build_vector_store
from rag_core.service import RAGService

_QUERY = "逾期付款违约金"
_TEXT = "买方逾期付款的，应按日支付合同总价千分之五的违约金"


def _settings(**overrides: Any) -> RagSettings:
    """构造离线配置（`_env_file=None` 避免 .env 干扰，与既有测试同款用法）。"""
    base: dict[str, Any] = {
        "vector_store": "inmemory",
        "embed_mode": "hash",
        "rerank_mode": "noop",
    }
    base.update(overrides)
    return RagSettings(_env_file=None, **base)


def _closed_port_uri() -> str:
    """返回一个**确定无人监听**的本地 URI（向 OS 借一个端口后立即释放）。

    ⚠️ 不能沿用配置默认值 `http://localhost:19530`：T-A3（`0-C2`）的交付物正是让 `19530`
    上**有** Milvus 服务，沿用默认值会让本用例随「Milvus 起没起」红绿翻转
    （2026-09-23 实测：起了服务后本用例 `DID NOT RAISE`）。这里改为构造一个必然关闭的
    端点，断言从此与宿主环境无关，硬过「服务不可达 → `StoreUnavailableError`」这一条。
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{probe.getsockname()[1]}"


def _chunk(seq: int = 0) -> ContractChunk:
    """构造最小合法切片。"""
    text = f"{_TEXT}（第 {seq + 1} 条）"
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id("DOC-1", f"条款{seq}", slice_idx=seq),
        doc_id="DOC-1",
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id=f"条款{seq}",
        seq_in_doc=seq,
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=0,
    )


def _service_with_chunks(
    cfg: RagSettings, count: int = 1
) -> tuple[RAGService, list[ContractChunk]]:
    """构造带 N 条切片的离线服务（用于观察阈值/截断是否生效）。"""
    service = build_service_from_settings(cfg)
    chunks = [_chunk(i) for i in range(count)]
    service.vector_store.ensure_collection(cfg.vector_dim)
    vectors = service.embedder.embed_batch([c.text for c in chunks])
    service.vector_store.upsert(
        [VectorRecord(chunk=c, vector=v) for c, v in zip(chunks, vectors)]
    )
    return service, chunks


# ===========================================================================
# 1. 三个配置项真正生效（断言 8）+ 请求优先
# ===========================================================================
def test_default_strategy_from_settings() -> None:
    """断言 8：`CG_RAG_STRATEGY` 改变服务级默认策略，且请求显式值优先。"""
    service = build_service_from_settings(
        _settings(strategy=RetrieveStrategy.HYBRID_RRF)
    )
    assert service._default_strategy is RetrieveStrategy.HYBRID_RRF

    implicit = service.retrieve(RAGQueryRequest(query=_QUERY))
    assert implicit.strategy is RetrieveStrategy.HYBRID_RRF, (
        "配置值未生效（仍是死配置）"
    )

    explicit = service.retrieve(
        RAGQueryRequest(query=_QUERY, strategy=RetrieveStrategy.DENSE_ONLY)
    )
    assert explicit.strategy is RetrieveStrategy.DENSE_ONLY, "请求显式值必须优先于配置"


def test_default_score_threshold_from_settings() -> None:
    """配置的阈值在请求未显式传时成为过滤下限（用空召回与否观察）。"""
    service, chunks = _service_with_chunks(_settings(score_threshold=0.999))
    blocked = service.retrieve(RAGQueryRequest(query=_QUERY))
    assert blocked.retrieved_docs == [] and blocked.degraded is True

    allowed = service.retrieve(RAGQueryRequest(query=_QUERY, score_threshold=0.0))
    assert [d.chunk_id for d in allowed.retrieved_docs] == [c.chunk_id for c in chunks]


def test_default_rerank_top_n_from_settings() -> None:
    """配置的重排保留条数在请求未显式传时生效。"""
    service, chunks = _service_with_chunks(_settings(rerank_top_n=1), count=3)
    resp = service.retrieve(RAGQueryRequest(query=_QUERY, score_threshold=0.0))
    assert len(resp.retrieved_docs) == 1, "服务级 rerank_top_n 未生效"

    resp2 = service.retrieve(
        RAGQueryRequest(query=_QUERY, score_threshold=0.0, rerank_top_n=3)
    )
    assert len(resp2.retrieved_docs) == len(chunks), "请求显式值必须优先"


# ===========================================================================
# 2. rerank_threshold 的边界（只在精排真正生效时叠加）
# ===========================================================================
def test_rerank_threshold_not_applied_on_noop_path() -> None:
    """noop 路径不下发精排阈值 —— 否则离线默认路径会被重复扣阈值打成空召回。"""
    service, chunks = _service_with_chunks(_settings(rerank_threshold=0.999))
    assert service._rerank_threshold is None

    resp = service.retrieve(RAGQueryRequest(query=_QUERY, score_threshold=0.0))
    assert [d.chunk_id for d in resp.retrieved_docs] == [c.chunk_id for c in chunks]


def test_rerank_threshold_applied_on_http_path() -> None:
    """http 精排且已配 URL 时下发配置值（只验证接线，不发真实请求）。"""
    cfg = _settings(
        rerank_mode="http",
        rerank_http_url="http://localhost:9/rerank",
        rerank_threshold=0.42,
    )
    service = build_service_from_settings(cfg)
    assert service._rerank_threshold == 0.42


def test_rerank_threshold_skipped_when_http_url_missing() -> None:
    """http 但缺 URL → 工厂会回退 noop → 同样不下发阈值。"""
    cfg = _settings(rerank_mode="http", rerank_http_url="", rerank_threshold=0.42)
    assert build_service_from_settings(cfg)._rerank_threshold is None


# ===========================================================================
# 3. 八项「死配置」的最终归属（断言 9）
# ===========================================================================
def test_t_a7_eight_items_have_consumers_or_annotation() -> None:
    """断言 9：八项逐条核对 —— 有消费者，或显式标注归属/未实现。"""
    fields = RagSettings.model_fields

    for name in ("strategy", "score_threshold", "rerank_threshold", "rerank_top_n"):
        assert "T-A7" in (fields[name].description or ""), f"{name} 未标明 T-A7 接线"

    for name in ("max_retries", "max_query_chars"):
        assert "T-B4" in (fields[name].description or ""), (
            f"{name} 未标明消费方归属（T-B4）"
        )

    assert "未实现" in (fields["cache_ttl_s"].description or ""), (
        "cache_ttl_s 未显式登记未实现"
    )

    assert "log_level" not in fields, "log_level 重复声明应已删除"


def test_max_context_tokens_description_mentions_both_consumers() -> None:
    """`timeout_ms` 的既有消费者不得被误删（文档 §⑧ 特别提醒它**不是**死配置）。"""
    assert "factory" in (RagSettings.model_fields["timeout_ms"].description or "")


# ===========================================================================
# 4. overrides 反射命名（§⑩-6）
# ===========================================================================
def test_overrides_reflection_naming() -> None:
    """新增可覆盖字段必须命名为 `_<同名>`，否则 overrides 静默失效。"""
    service = build_service_from_settings(_settings())
    for key in (
        "default_strategy",
        "default_score_threshold",
        "rerank_top_n",
        "rerank_threshold",
    ):
        assert hasattr(service, f"_{key}"), (
            f"缺少私有字段 _{key}（overrides 会静默失效）"
        )

    patched = build_service_from_settings(_settings(), default_score_threshold=0.11)
    assert patched._default_score_threshold == 0.11


# ===========================================================================
# 5. T-A2（`0-C1`）：Milvus 配置接线
# ===========================================================================
def test_build_vector_store_milvus(fake_pymilvus: dict[str, Any]) -> None:
    """§⑦-1~3：三分支各返回正确类型（milvus 用假 SDK，**不建连**）。"""
    from rag_contracts.ports import VectorStorePort
    from rag_core.adapters import (
        InMemoryVectorStore,
        MilvusVectorStore,
        QdrantVectorStore,
    )

    milvus = build_vector_store(_settings(vector_store="milvus"))
    assert isinstance(milvus, MilvusVectorStore)
    assert isinstance(milvus, VectorStorePort)
    assert isinstance(build_vector_store(_settings()), InMemoryVectorStore)
    assert isinstance(
        build_vector_store(
            _settings(vector_store="qdrant", qdrant_mode="local_memory")
        ),
        QdrantVectorStore,
    )


def test_build_vector_store_invalid_raises() -> None:
    """§⑦-4：非法取值抛 `ValueError` 且消息含该取值（用 model_copy 绕过 Literal 校验）。"""
    bogus = RagSettings(_env_file=None).model_copy(update={"vector_store": "bogus"})
    with pytest.raises(ValueError) as excinfo:
        build_vector_store(bogus)
    assert "bogus" in str(excinfo.value)


def test_vector_store_literal_values() -> None:
    """§⑦-5：`Literal` 含 3 个取值、默认仍 `inmemory`（离线默认路径与回退路径都在）。"""
    field = RagSettings.model_fields["vector_store"]
    assert set(get_args(field.annotation)) == {"inmemory", "qdrant", "milvus"}
    assert field.default == "inmemory"
    assert RagSettings().vector_store == "inmemory"


def test_milvus_settings_defaults() -> None:
    """§⑦-7：5 个 `milvus_*` 字段默认值（与 `0-C1 §⑤` / `1-3 §⑤` 逐字一致）。"""
    settings = RagSettings()
    assert settings.milvus_mode == "server"
    assert settings.milvus_uri == "http://localhost:19530"
    assert settings.milvus_token == ""
    assert settings.milvus_db_name == "default"
    assert settings.milvus_timeout_s == 5.0
    assert RagSettings.model_fields["milvus_timeout_s"].metadata, "ge=0.1 约束应保留"


def test_milvus_branch_reuses_collection(fake_pymilvus: dict[str, Any]) -> None:
    """§⑥-3 / §⑧-5：Milvus 复用 `settings.collection`，不得另立 collection 配置项。"""
    settings = _settings(vector_store="milvus", collection="shared_collection")
    store = build_vector_store(settings)

    assert store._collection == "shared_collection"
    assert "collection" not in {
        name for name in RagSettings.model_fields if name.startswith("milvus_")
    }


def test_milvus_branch_forwards_connection_params(
    fake_pymilvus: dict[str, Any],
) -> None:
    """§⑥-5：`milvus_*` 真的透传给适配器/SDK（`timeout_s` 不能是死配置）。"""
    settings = _settings(
        vector_store="milvus",
        milvus_uri="http://milvus-host:19530",
        milvus_token="user:pwd",
        milvus_db_name="rag",
        milvus_timeout_s=7.5,
    )
    build_vector_store(settings)

    kwargs = fake_pymilvus["client"].kwargs
    assert kwargs["uri"] == "http://milvus-host:19530"
    assert kwargs["db_name"] == "rag"
    assert kwargs["token"] == "user:pwd"
    assert kwargs["timeout"] == 7.5


def test_milvus_branch_raises_store_unavailable_without_server() -> None:
    """§⑦-1 的可执行替身：**无假 SDK 且无服务**时，milvus 分支必须走适配器并抛
    `StoreUnavailableError`（而不是 `ValueError` —— 后者说明分支根本没接上）。

    ⚠️ 卡内 §⑦-1 写的是"`type(...) is MilvusVectorStore`（不建连）"；实测
    `MilvusVectorStore.__init__` **构造即建连**（`1-2 §⑪.3-#3`），无服务时必然失败。
    故接线断言改为"假 SDK 验类型（上一条）+ 真路径验异常类型（本条）"两条。

    T-A3（`0-C2`）起服务后，原来的 `CG_RAG_TEST_MILVUS_URI` skip 守卫与 `19530` 默认值
    都不再成立（服务就在 `19530` 上）→ 改为指向 `_closed_port_uri()` 构造的关闭端口，
    于是**不再需要 skip**，用例无条件执行且与宿主环境无关。
    """
    from rag_contracts.errors import StoreUnavailableError

    with pytest.raises(StoreUnavailableError):
        build_vector_store(
            _settings(vector_store="milvus", milvus_uri=_closed_port_uri())
        )


def test_milvus_branch_uses_delayed_import() -> None:
    """§⑨-2：`factory.py` **顶层**不得急切导入 Milvus 适配器/`pymilvus`。

    否则未装 `pymilvus` 时整个工厂（乃至 `rag_core`）都 import 失败，离线默认路径陪葬。
    用 AST 只查模块级 import，函数体内的延迟导入不受影响。
    """
    assert factory_module.__file__ is not None
    tree = ast.parse(Path(factory_module.__file__).read_text(encoding="utf-8"))
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names = [alias.name for alias in node.names]
            top_level.extend(names)
            if node.module:
                top_level.append(node.module)

    assert "MilvusVectorStore" not in top_level, (
        "factory.py 顶层 import 了 MilvusVectorStore；应改为 build_vector_store() 函数内延迟导入"
    )
    assert "milvus_store" not in top_level
    assert not [m for m in top_level if m.startswith("pymilvus")]
