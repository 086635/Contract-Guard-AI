"""Milvus 向量库适配器：实现 `rag_contracts.ports.VectorStorePort`。

设计要点：
1. `pymilvus` **仅在本模块内延迟导入** —— 默认路径（InMemoryVectorStore）零影响，
   与 `qdrant_store.py` 的既有约定一致。
2. `chunk_id` 直接作 Milvus 主键（VARCHAR），与 Qdrant 的 point_id 语义一致。
3. payload 双层存储：完整切片存 JSON 字段 `payload`；**参与过滤的 6 个字段冗余成顶层标量列**
   （Milvus 无法高效过滤 JSON 嵌套键，必须冗余，见 1-2 §⑤）。
4. 所有异常统一包装为 `StoreUnavailableError`，与既有适配器一致 —— `RAGService.retrieve()`
   据此走降级路径（红线：retrieve 永不抛异常）。
5. 🔴 **不得成为任何默认值**：本类不出现在 `RagSettings.vector_store` 的默认值与
   `factory.build_vector_store()` 的兜底分支里（默认仍是 `inmemory`，见决策 D1）。

对应：RAG_MODULE_DESIGN.md §1.3.1（MilvusVectorStore 为预留命名）；文件集合/codex/1-2Milvus适配器.md
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from cg_observability import get_logger, log_event
from rag_contracts.errors import StoreUnavailableError
from rag_contracts.ports import VectorHit, VectorRecord
from rag_contracts.query import RetrievalFilters

_LOG = get_logger("rag_core.adapters.milvus_store")

# 主键列名：pymilvus 3.0 的 search 命中字典以**主键字段名**为键（不是 `id`），
# 故写入 schema 与读取命中必须共用同一常量（见 1-2 §⑤ 的 `chunk_id`）。
_PK_FIELD: str = "chunk_id"

# 需建标量列 / 参与过滤的字段。
# ⚠️ 语义必须与 qdrant_store.KEYWORD_INDEX_FIELDS 完全一致，两边字段名不得漂移
# （由 tests/test_milvus_store.py 的守卫用例锁死）。
_SCALAR_FIELDS: tuple[str, ...] = (
    "source_type",
    "chunk_type",
    "contract_type",
    "doc_id",
    "category",
    "law_code",
)

# VARCHAR 列宽：chunk_id 是 uuid5(36)，其余按 ContractChunk 的 max_length + 余量
_MAX_LEN: dict[str, int] = {
    "chunk_id": 64,
    "doc_id": 200,
    "source_type": 32,
    "chunk_type": 32,
    "contract_type": 32,
    "law_code": 64,
    "category": 64,
}

# HNSW 索引参数（与 Qdrant 侧 m=16 / ef_construct=200 保持同等召回水平）
_HNSW_PARAMS: dict[str, int] = {"M": 16, "efConstruction": 200}
_SEARCH_EF: int = 64


class MilvusVectorStore:
    """Milvus 适配器（mode: server / lite）。

    Attributes:
        name: 适配器标识，写入日志 `store` 字段。
    """

    def __init__(
        self,
        *,
        mode: str = "server",
        uri: str = "http://localhost:19530",
        token: str = "",
        db_name: str = "default",
        collection: str = "contract_clauses",
        timeout_s: float = 5.0,
    ) -> None:
        """初始化客户端（构造时即建连）。

        Args:
            mode: `server` 连服务 / `lite` 本地文件（Windows 上不可用，见 1-4）。
            uri: server 模式为服务地址；lite 模式为本地 .db 文件路径。
            token: `user:password` 或 API Key；为空则不传该参数。
            db_name: Milvus 数据库名。
            collection: 集合名。
            timeout_s: 请求超时（秒）。

        Raises:
            StoreUnavailableError: pymilvus 未安装、mode 非法或服务不可达。
        """
        self._mode = mode
        self._collection = collection
        self.name: str = f"milvus:{mode}"
        self._client: Any = self._build_client(mode, uri, token, db_name, timeout_s)

    @staticmethod
    def _build_client(
        mode: str, uri: str, token: str, db_name: str, timeout_s: float
    ) -> Any:
        """构造 MilvusClient。

        Args:
            mode: 运行模式。
            uri: 服务地址或本地文件路径。
            token: 认证串。
            db_name: 数据库名。
            timeout_s: 超时秒数。

        Returns:
            MilvusClient 实例。

        Raises:
            StoreUnavailableError: 依赖缺失、mode 非法或连接失败。
        """
        try:
            # 延迟导入，避免污染默认路径；pymilvus 无 py.typed，故需忽略 untyped 告警
            from pymilvus import MilvusClient  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - 仅在缺包环境触发
            raise StoreUnavailableError(
                "未安装 pymilvus，请改用 CG_RAG_VECTOR_STORE=inmemory 或安装该包"
            ) from exc

        if mode not in ("server", "lite"):
            raise StoreUnavailableError(f"不支持的 milvus mode：{mode}")

        try:
            if mode == "lite":
                # ⚠️ Milvus Lite 官方支持以 Linux/macOS 为主；Windows 请用 server 模式
                return MilvusClient(uri=uri, timeout=timeout_s)
            kwargs: dict[str, Any] = {
                "uri": uri,
                "db_name": db_name,
                "timeout": timeout_s,
            }
            if token:
                kwargs["token"] = token
            return MilvusClient(**kwargs)
        except Exception as exc:
            # ⚠️ 实测：pymilvus 3.0 的 MilvusClient **构造即建连**，服务不可达会直接抛
            # MilvusException —— 统一包装成 StoreUnavailableError，避免裸异常逃出适配层
            raise StoreUnavailableError(f"Milvus 连接失败：{exc}") from exc

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _escape(value: str) -> str:
        """转义 expr 字符串字面量（防注入 / 防语法错）。

        Args:
            value: 原始字符串值。

        Returns:
            转义后的字符串（反斜杠与双引号已转义）。
        """
        return value.replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _build_expr(filters: RetrievalFilters | None) -> str:
        """把 RetrievalFilters 翻译为 Milvus `expr` 布尔表达式。

        与 `qdrant_store._build_filter` 语义等价，仅语法不同。
        ⚠️ 字段名**不得加引号**（实测：`"field" in [...]` 会被解析器拒绝，
        报 `left side of 'in' must be a field reference`）。
        设计为 `@staticmethod`：不依赖连接，离线即可单测（任务文档 §⑨）。

        Args:
            filters: 契约层过滤条件。

        Returns:
            expr 字符串；无过滤条件时返回空字符串。
        """
        if filters is None:
            return ""
        escape = MilvusVectorStore._escape
        clauses: list[str] = []
        if filters.source_types:
            vals = ", ".join(f'"{escape(s.value)}"' for s in filters.source_types)
            clauses.append(f"source_type in [{vals}]")
        if filters.chunk_types:
            vals = ", ".join(f'"{escape(c.value)}"' for c in filters.chunk_types)
            clauses.append(f"chunk_type in [{vals}]")
        if filters.contract_type:
            clauses.append(f'contract_type == "{escape(filters.contract_type.value)}"')
        if filters.law_codes:
            vals = ", ".join(f'"{escape(c)}"' for c in filters.law_codes)
            clauses.append(f"law_code in [{vals}]")
        if filters.doc_ids:
            vals = ", ".join(f'"{escape(d)}"' for d in filters.doc_ids)
            clauses.append(f"doc_id in [{vals}]")
        if filters.categories:
            vals = ", ".join(f'"{escape(c)}"' for c in filters.categories)
            clauses.append(f"category in [{vals}]")
        return " and ".join(clauses)

    @staticmethod
    def _row_of(record: VectorRecord) -> dict[str, Any]:
        """把 VectorRecord 摊平成 Milvus 一行（含冗余标量列）。

        Args:
            record: （chunk, vector）记录。

        Returns:
            可直接喂给 `MilvusClient.upsert(data=[...])` 的字典。
        """
        chunk = record.chunk
        row: dict[str, Any] = {
            _PK_FIELD: chunk.chunk_id,
            "vector": list(record.vector),
            "payload": chunk.to_payload(),  # 完整切片，供 from_payload 还原
        }
        for field in _SCALAR_FIELDS:
            value = getattr(chunk, field, None)
            # None 在 Milvus 标量列中写作 ""，读取时由 ContractChunk 解释
            row[field] = getattr(value, "value", value) if value is not None else ""
        return row

    @staticmethod
    def _score_from_distance(distance: float) -> float:
        """把 Milvus 的 `distance` 换算成契约要求的相似度（越大越相似）。

        ⚠️ **实测（2026-09-22，pymilvus 3.0.0 + milvus-lite 3.0）**：`metric_type=COSINE`
        时 Milvus 返回的是**余弦距离** `1 - cos`（同向量自检索 = 0.0、正交 = 1.0、
        反向 = 2.0，结果按该值**升序**返回），与 `1-2 §⑤` 的预期（distance 即相似度）
        相反。契约 `VectorHit.score` 的定义是「相似度得分（COSINE，越大越相似）」，
        故此处统一反向映射，保证与 `QdrantVectorStore` / `InMemoryVectorStore` 同口径。

        Args:
            distance: Milvus 命中里的 `distance` 字段。

        Returns:
            余弦相似度，落在 [-1, 1]。
        """
        return 1.0 - float(distance)

    # ---------------- VectorStorePort 实现 ----------------
    def ensure_collection(self, dim: int) -> None:
        """建集合 + 索引，幂等。

        Args:
            dim: 向量维度（项目锁定 1024）。

        Raises:
            StoreUnavailableError: 建集合或加载失败。
        """
        from pymilvus import DataType

        try:
            if self._client.has_collection(self._collection):
                # 已存在：跨进程 / 重启后集合可能处于 released 状态，
                # search 会报 `Collection ... is in state 'released'`（实测 code=101）
                self._client.load_collection(self._collection)
                return

            schema = self._client.create_schema(
                auto_id=False, enable_dynamic_field=False
            )
            schema.add_field(
                _PK_FIELD,
                DataType.VARCHAR,
                is_primary=True,
                max_length=_MAX_LEN[_PK_FIELD],
            )
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=int(dim))
            schema.add_field("payload", DataType.JSON)
            for field in _SCALAR_FIELDS:
                schema.add_field(
                    field, DataType.VARCHAR, max_length=_MAX_LEN.get(field, 200)
                )

            index_params = self._client.prepare_index_params()
            index_params.add_index(
                field_name="vector",
                index_type="HNSW",
                metric_type="COSINE",
                params=dict(_HNSW_PARAMS),
            )
            # 🔴 标量列**不建** INVERTED 索引：实测 pymilvus 3.0.0 + milvus-lite 3.0 的
            # 索引翻译层强制要求每个索引都给 metric_type，而引擎侧只接受向量字段
            # （`create_index only supports vector fields`）→ 带标量索引的 create_collection
            # 会整体失败。按 1-2 §⑦-5「不支持则去掉该循环（性能降级但功能不受影响）」处理：
            # expr 过滤仍正确，只是不走索引（见 `1.C5` §⑥/§⑦ 的实测后回填）。
            # 传 index_params 会顺带完成 load，search 前无需再手动 load
            self._client.create_collection(
                self._collection, schema=schema, index_params=index_params
            )
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus 建集合失败：{exc}") from exc

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """幂等写入：主键为 chunk_id，重复写覆盖不产生副本。

        Args:
            records: （chunk, vector）序列。

        Returns:
            写入条数。

        Raises:
            StoreUnavailableError: 写入失败。
        """
        if not records:
            return 0
        rows = [self._row_of(r) for r in records]
        start = time.perf_counter()
        try:
            self._client.upsert(collection_name=self._collection, data=rows)
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus upsert 失败：{exc}") from exc
        log_event(
            _LOG,
            "store.upsert.done",
            backend="milvus",
            count=len(rows),
            latency_ms=round((time.perf_counter() - start) * 1000, 3),
        )
        return len(rows)

    def search(
        self,
        vector: Sequence[float],
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[VectorHit]:
        """稠密检索（COSINE）。

        Args:
            vector: 查询向量。
            top_k: 返回条数上限。
            filters: 过滤条件，None 表示不过滤。

        Returns:
            按得分降序的命中列表。

        Raises:
            StoreUnavailableError: 检索失败。
        """
        start = time.perf_counter()
        try:
            results = self._client.search(
                collection_name=self._collection,
                data=[list(vector)],
                limit=max(1, int(top_k)),
                filter=self._build_expr(filters),
                output_fields=["payload"],
                search_params={"metric_type": "COSINE", "params": {"ef": _SEARCH_EF}},
            )
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus search 失败：{exc}") from exc

        hits: list[VectorHit] = []
        for row in results[0] if results else []:
            entity = row.get("entity") or {}
            payload = entity.get("payload") or {}
            hits.append(
                VectorHit(
                    chunk_id=str(row.get(_PK_FIELD) or ""),
                    score=self._score_from_distance(row.get("distance", 0.0)),
                    payload=dict(payload),
                )
            )
        log_event(
            _LOG,
            "store.search.done",
            backend="milvus",
            count=len(hits),
            latency_ms=round((time.perf_counter() - start) * 1000, 3),
        )
        return hits

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除。

        ✅ 优于 Qdrant 版：用表达式删除，**没有**「单次 scroll 上限 10000 条」的限制。
        ⚠️ 实测 pymilvus 3.0.0 的返回形态有两种：服务端返回主键时是 **list[str]**，
        否则是 `{"delete_count": N, "cost": ...}`（零值会被 OmitZeroDict 省略）。

        Args:
            doc_id: 源文档标识。

        Returns:
            删除条数（取不到计数时返回 0）。

        Raises:
            StoreUnavailableError: 删除失败。
        """
        expr = f'doc_id == "{self._escape(doc_id)}"'
        try:
            result = self._client.delete(collection_name=self._collection, filter=expr)
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus delete 失败：{exc}") from exc
        if isinstance(result, dict):
            return int(result.get("delete_count") or 0)
        if isinstance(result, (list, tuple)):
            return len(result)
        return int(getattr(result, "delete_count", 0) or 0)

    def health(self) -> bool:
        """健康检查，供降级判断。

        Returns:
            True 表示可用。
        """
        try:
            self._client.list_collections()
            return True
        except Exception:  # noqa: BLE001 - 健康检查失败即不可用，不抛
            return False

    def count(self) -> int:
        """当前集合内**存活**实体数（对齐 `QdrantVectorStore.count` 的 `exact=True` 口径）。

        ⚠️ **实测（2026-09-23，真 Milvus server v2.5.4，见 `1-2 §⑪.6`）**：
        `get_collection_stats()["row_count"]` **不能用** —— 它是**物理行数**，有两处错：
          ① 含**逻辑删除**的行（写 3 条 → 删 3 条 → `flush()` 后仍返回 **3**）；
          ② 未 `flush()` 前恒为 **0**（`upsert` 3 条后立刻查也是 0）。
        改用 `count(*)` 聚合查询 + `consistency_level="Strong"`：实测
        「`upsert` 3 → **3**；同主键再 `upsert` → 仍 **3**；`delete` 全部 → **0**」，
        **无需 flush** 即为正确的存活数。

        ⚠️ 这里刻意**不**调 `flush()`：`flush()` 是重操作（落盘 + 封段），
        且会让「同一 `chunk_id` 重复写入 → `count()` 不增长」这类断言依赖副作用。

        Returns:
            存活实体数；集合不存在时返回 0。
        """
        try:
            rows = self._client.query(
                collection_name=self._collection,
                filter="",
                output_fields=["count(*)"],
                consistency_level="Strong",
            )
        except Exception:  # noqa: BLE001 - 集合不存在时返回 0
            return 0
        if not rows:
            return 0
        first = rows[0]
        # 聚合结果形如 [{"count(*)": N}]；不同版本可能退化为标量，故两种都兼容
        if isinstance(first, dict):
            return int(first.get("count(*)") or 0)
        return int(first or 0)


__all__ = ["MilvusVectorStore"]
