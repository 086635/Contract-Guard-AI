# 1-2 · Milvus 适配器（T-A1）

> 所属模块：1 · RAG　公共规范见 `0.施工标准与定义归属.md`

> 🔵 **本任务定位（2026-09-21 修订）**：这是一个**备选适配器**任务，属 **B 组 · 可扩展性支线（P2）**。
> **它不阻塞任何关键路径**，也不改变主力向量库 —— 生产/演示仍是 **Qdrant**（见 `1.RAG模块.md` §1.1.3）。
> 做它的目的只有一个：**证明 Ports & Adapters 的可插拔能力**（换库 = 加文件 + 加分支，业务层零改动）。
> 时间紧张时可整体推迟，**不影响赛题达标**。

## ① 元信息

| 项 | 值 |
|---|---|
| 任务 ID | **T-A1** |
| 优先级 | **P2**（**B 组 · 可扩展性支线**，不阻塞达标） |
| 人日 | 1.5 |
| 前置依赖 | ~~决策 D1~~（✅ **已拍板：Qdrant 主力 + Milvus 备选**，见 `1.RAG模块.md` §1.1.3 / §1.7.1）；T-A3（容器可起）便于联调 |
| 影响面 | 仅新增 1 个文件 + 1 行包导出 |
| 是否触碰 `service.py` | ❌ **否**（这正是本任务能低成本完成的原因） |
| 是否触碰契约层 | ❌ **否**（`VectorHit` / `VectorRecord` / `RetrievalFilters` 全部沿用） |

---

## ② 范围

### ✅ 做

- 在 `packages/rag-core/rag_core/adapters/milvus_store.py` 新建 `MilvusVectorStore`
- 实现 `VectorStorePort` 的 5 个方法：`ensure_collection` / `upsert` / `search` / `delete_by_doc` / `health`
- 额外提供 `count()`（对齐 `QdrantVectorStore.count` 的既有约定）
- 在 `adapters/__init__.py` 导出

### ❌ 不做

- ❌ **不改 `service.py`**（业务编排与存储实现解耦是既有架构的核心优势）
- ❌ **不改 `rag_contracts`**（`VectorHit.payload` 保持 `dict`）
- ❌ **不删 `QdrantVectorStore`**（它是双跑校验的基准 + 回退路径，且已有测试覆盖）
- ❌ **不做 Milvus 原生稀疏向量 / BM25 内置函数**（属备选方案 B，见 `codex项目推进.md` §1 · D1.5；本模块决策登记见 `1.RAG模块.md` §1.7，本次不做）
- ❌ **不写数据迁移**（属 T-A10，见 `1-5集成测试与迁移.md`）

---

## ③ 定义归属

| 本文件**定义** | 本文件**引用**（不重复定义） |
|---|---|
| `MilvusVectorStore` 类（唯一处：`adapters/milvus_store.py`） | `VectorStorePort`（`rag_contracts/ports.py`） |
| Milvus 侧的表结构、索引参数、`expr` 构造 | `VectorRecord` / `VectorHit`（`rag_contracts/ports.py`） |
| Milvus 专属常量：`_SCALAR_FIELDS` / `_MAX_LEN` | `RetrievalFilters`（`rag_contracts/query.py`） |
| — | `StoreUnavailableError`（`rag_contracts/errors.py`） |
| — | `ContractChunk.to_payload()`（`rag_contracts/chunk.py`） |

> ⚠️ `_SCALAR_FIELDS` 的**语义**必须与 `qdrant_store.KEYWORD_INDEX_FIELDS` 保持一致
> （同样的 6 个字段名，只是两个适配器各自声明）。**两边字段名不得漂移**，
> 否则同一个 `RetrievalFilters` 在两个库上会过滤出不同结果。

---

## ④ 交付物

| 文件 | 动作 | 职责 |
|---|---|---|
| `packages/rag-core/rag_core/adapters/milvus_store.py` | **新建** | Milvus 适配器实现 |
| `packages/rag-core/rag_core/adapters/__init__.py` | 修改 | 导出 `MilvusVectorStore` |
| `packages/rag-core/tests/test_milvus_store.py` | **新建** | 集成测试（见 `1-5集成测试与迁移.md`） |

> ⚠️ 配置字段与工厂分支**不在本任务**，属 `1-3配置与工厂接线.md`（T-A2）。

**`adapters/__init__.py` 的导出方式（照抄既有模式，勿自创）**：

本包对重型 SDK 适配器统一用 **PEP 562 懒导出**
（现有的 `QdrantVectorStore` / `HttpReranker` 就是这么做的）。
`MilvusVectorStore` 必须走同一模式，共三处：

| 位置 | 要改什么 |
|---|---|
| 顶部 `if TYPE_CHECKING:` 块 | 加 `from .milvus_store import MilvusVectorStore`（仅供类型检查器；缺它 mypy 会把它推断成 `object`，调用处报 `"object" not callable`） |
| `__all__` | 加 `"MilvusVectorStore"`（**按字母序**，插在 `"InMemoryVectorStore"` 与 `"NoopReranker"` 之间） |
| `__getattr__()` | 加一条 `if name == "MilvusVectorStore":`，内部 `from .milvus_store import MilvusVectorStore` 后 `return` |

> 🔴 **不要**在 `__init__.py` 顶层写 `from .milvus_store import MilvusVectorStore`
> —— 这是 §⑩ 避坑第 5 条的**另一半**：
> 模块内的延迟导入只保护 `milvus_store.py` 自身，
> **包出口的急切导入会让整个 `adapters` 包 import 失败**，默认离线路径随即失效。

---

## ⑤ 接口规格 · 字段映射（**本任务的核心技术内容**）

Milvus 与 Qdrant 的模型差异**必须在适配器内部消化**，不得泄漏到契约层。

| 契约概念 | Qdrant 做法（已存在） | Milvus 做法（本任务） | 注意 |
|---|---|---|---|
| `chunk_id`（主键） | `PointStruct.id`（字符串） | `DataType.VARCHAR(is_primary=True, max_length=64)` | uuid5 为 36 字符，64 足够；**VARCHAR 主键必须显式给 `max_length`** |
| `ContractChunk` 全字段 | `payload=chunk.to_payload()`（任意 JSON） | **1 个 JSON 字段 `payload`** + **6 个扁平标量列** | ⚠️ **Milvus 不能高效过滤 JSON 嵌套键**，而 `RetrievalFilters` 要按 6 个字段过滤 → **必须冗余成顶层标量列** |
| 过滤条件 | `models.Filter(must=[FieldCondition(...)])` | `expr` 字符串 DSL（`source_type in [...] and ...`） | ⚠️ **必须转义字符串字面量**，否则语法错或注入 |
| 相似度 | `point.score` | `hit["distance"]` | ⚠️ `metric_type=COSINE` 时 `distance` **即相似度（越大越相似）**，与 Qdrant 语义一致 |
| 删除 | `scroll` 找 id → `delete(PointIdsList)` | `delete(filter='doc_id == "..."')` | ✅ **Milvus 更优**：Qdrant 版有「单次 scroll 上限 10000 条、超限残留孤儿向量」的已知限制，Milvus 用表达式删除天然没有 |
| 建集合 | `create_collection` + `create_payload_index`（幂等靠 try/except） | `has_collection()` 判断 + `create_collection(schema, index_params)` | ✅ **Milvus 更优**：有原生 `has_collection()`，无需吞异常 |
| 加载 | 不需要 | `search` 前须 collection 已 load | 传 `index_params` 给 `create_collection` 会**顺带完成 load** |
| 健康检查 | `get_collections()` | `list_collections()` | 仅探测连通性 |

### 关键设计决策

1. **`payload` 保持为完整 JSON**，使 `ContractChunk.from_payload(hit.payload)` 零改动可用
2. **6 个标量列与 JSON 字段冗余并存** —— 冗余是为了过滤性能，JSON 是为了契约保真
3. **标量列的空值统一写 `""`**（而非 `None`），避免 schema 对 nullable 的版本差异问题；
   读取时由 `ContractChunk` 自身的校验规则解释

---

## ⑥ 实现规范 · 参考骨架（可直接据此实现）

```python
"""Milvus 向量库适配器：实现 `rag_contracts.ports.VectorStorePort`。

设计要点：
1. `pymilvus` **仅在本模块内延迟导入** —— 默认路径（InMemoryVectorStore）零影响，
   与 `qdrant_store.py` 的既有约定一致。
2. `chunk_id` 直接作 Milvus 主键（VARCHAR），与 Qdrant 的 point_id 语义一致。
3. payload 双层存储：完整切片存 JSON 字段 `payload`；**参与过滤的 6 个字段冗余成顶层标量列**
   （Milvus 无法高效过滤 JSON 嵌套键，必须冗余，见 1-2 §⑤）。
4. 所有异常统一包装为 `StoreUnavailableError`，与既有适配器一致 —— `RAGService.retrieve()`
   据此走降级路径（红线：retrieve 永不抛异常）。

对应：RAG_MODULE_DESIGN.md §1.3.1（MilvusVectorStore 为预留命名）；文件集合/codex/1-2Milvus适配器.md
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag_contracts.errors import StoreUnavailableError
from rag_contracts.ports import VectorHit, VectorRecord
from rag_contracts.query import RetrievalFilters

# 需建标量索引 / 参与过滤的字段。
# ⚠️ 语义必须与 qdrant_store.KEYWORD_INDEX_FIELDS 完全一致，两边字段名不得漂移。
_SCALAR_FIELDS: tuple[str, ...] = (
    "source_type",
    "chunk_type",
    "contract_type",
    "doc_id",
    "law_code",
    "category",
)

# VARCHAR 列宽：chunk_id 是 uuid5(36)，其余按业务上限留足余量
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
        """初始化客户端（延迟到构造时才建连）。

        Args:
            mode: `server` 连服务 / `lite` 本地文件（Windows 上不可用，见 1-4）。
            uri: server 模式为服务地址；lite 模式为本地 .db 文件路径。
            token: `user:password` 或 API Key；为空则不传该参数。
            db_name: Milvus 数据库名。
            collection: 集合名。
            timeout_s: 请求超时（秒）。

        Raises:
            StoreUnavailableError: pymilvus 未安装或参数非法。
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
            StoreUnavailableError: 依赖缺失或 mode 非法。
        """
        try:
            from pymilvus import MilvusClient  # 延迟导入，避免污染默认路径
        except ImportError as exc:  # pragma: no cover - 仅在缺包环境触发
            raise StoreUnavailableError(
                "未安装 pymilvus，请改用 CG_RAG_VECTOR_STORE=inmemory 或安装该包"
            ) from exc

        if mode == "lite":
            # ⚠️ Milvus Lite 官方支持以 Linux/macOS 为主；Windows 请用 server 模式
            return MilvusClient(uri=uri)
        if mode == "server":
            kwargs: dict[str, Any] = {"uri": uri, "db_name": db_name}
            if token:
                kwargs["token"] = token
            return MilvusClient(**kwargs)
        raise StoreUnavailableError(f"不支持的 milvus mode：{mode}")

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

    def _build_expr(self, filters: RetrievalFilters | None) -> str:
        """把 RetrievalFilters 翻译为 Milvus `expr` 布尔表达式。

        与 `qdrant_store._build_filter` 语义等价，仅语法不同。

        Args:
            filters: 契约层过滤条件。

        Returns:
            expr 字符串；无过滤条件时返回空字符串。
        """
        if filters is None:
            return ""
        clauses: list[str] = []
        if filters.source_types:
            vals = ", ".join(f'"{self._escape(s.value)}"' for s in filters.source_types)
            clauses.append(f"source_type in [{vals}]")
        if filters.chunk_types:
            vals = ", ".join(f'"{self._escape(c.value)}"' for c in filters.chunk_types)
            clauses.append(f"chunk_type in [{vals}]")
        if filters.contract_type:
            clauses.append(
                f'contract_type == "{self._escape(filters.contract_type.value)}"'
            )
        if filters.law_codes:
            vals = ", ".join(f'"{self._escape(c)}"' for c in filters.law_codes)
            clauses.append(f"law_code in [{vals}]")
        if filters.doc_ids:
            vals = ", ".join(f'"{self._escape(d)}"' for d in filters.doc_ids)
            clauses.append(f"doc_id in [{vals}]")
        if filters.categories:
            vals = ", ".join(f'"{self._escape(c)}"' for c in filters.categories)
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
            "chunk_id": chunk.chunk_id,
            "vector": list(record.vector),
            "payload": chunk.to_payload(),  # 完整切片，供 from_payload 还原
        }
        for field in _SCALAR_FIELDS:
            value = getattr(chunk, field, None)
            # None 在 Milvus 标量列中写作 ""，读取时由 ContractChunk 解释
            row[field] = getattr(value, "value", value) if value is not None else ""
        return row

    # ---------------- VectorStorePort 实现 ----------------
    def ensure_collection(self, dim: int) -> None:
        """建集合 + 索引，幂等。

        Args:
            dim: 向量维度（项目锁定 1024）。

        Raises:
            StoreUnavailableError: 建集合失败。
        """
        from pymilvus import DataType

        if self._client.has_collection(self._collection):
            return

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            "chunk_id",
            DataType.VARCHAR,
            is_primary=True,
            max_length=_MAX_LEN["chunk_id"],
        )
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=int(dim))
        schema.add_field("payload", DataType.JSON)
        for field in _SCALAR_FIELDS:
            schema.add_field(field, DataType.VARCHAR, max_length=_MAX_LEN.get(field, 200))

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            params=dict(_HNSW_PARAMS),
        )
        for field in _SCALAR_FIELDS:
            # INVERTED 标量索引：让 expr 过滤走索引而非全表扫
            index_params.add_index(field_name=field, index_type="INVERTED")

        try:
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
        try:
            self._client.upsert(collection_name=self._collection, data=rows)
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus upsert 失败：{exc}") from exc
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
        try:
            results = self._client.search(
                collection_name=self._collection,
                data=[list(vector)],
                limit=max(1, int(top_k)),
                filter=self._build_expr(filters) or "",
                output_fields=["payload"],
                search_params={"metric_type": "COSINE", "params": {"ef": _SEARCH_EF}},
            )
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus search 失败：{exc}") from exc

        hits: list[VectorHit] = []
        for row in results[0] if results else []:
            # metric_type=COSINE 时 Milvus 的 distance 即相似度（越大越相似），与 Qdrant 语义一致
            entity = row.get("entity") or {}
            payload = entity.get("payload") or {}
            hits.append(
                VectorHit(
                    chunk_id=str(row.get("id", "")),
                    score=float(row.get("distance", 0.0)),
                    payload=dict(payload),
                )
            )
        return hits

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除。

        ✅ 优于 Qdrant 版：用表达式删除，**没有**「单次 scroll 上限 10000 条」的限制。

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
            return int(result.get("delete_count", 0) or 0)
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
        """当前集合内实体数（对齐 QdrantVectorStore.count）。

        Returns:
            实体数；集合不存在时返回 0。
        """
        try:
            stats = self._client.get_collection_stats(self._collection)
            return int(stats.get("row_count", 0))
        except Exception:  # noqa: BLE001 - 集合不存在时返回 0
            return 0


__all__ = ["MilvusVectorStore"]
```

---

## ⑦ 必须实测确认的 6 个点（**不要照抄后就认为对**）

pymilvus 各版本间存在差异，以下 6 点**必须在你的实际版本上验证**：

| # | 待确认 | 验证方法 |
|---|---|---|
| 1 | `MilvusClient` 的 `token` / `db_name` 参数名与默认值 | `inspect.signature(MilvusClient.__init__)` |
| 2 | `client.delete()` 的**返回结构**（dict 还是对象；字段名是否 `delete_count`） | 实跑一次删除并 `print(repr(result))` |
| 3 | `client.search()` 返回的 `entity` 能否取到 JSON 字段 | 实跑并打印原始返回 |
| 4 | `metric_type=COSINE` 时 `distance` 的含义 | **用已知向量做正反向验证**（同一向量自检索应接近 1.0） |
| 5 | `index_params.add_index(index_type="INVERTED")` 是否被目标版本支持 | 看 `create_collection` 是否报错；不支持则去掉该循环（性能降级但功能不受影响） |
| 6 | VARCHAR 主键 `max_length` 边界 | uuid5 为 36 字符，64 安全；**不要设成 32** |

> 💡 **强烈建议先跑最小脚本**：建集合 → 写 3 条 → 查 1 条 → 删 1 条 → `count()`。
> **不要在适配器未验证的情况下直接跑端到端** —— 那时错误会混进 `retrieve()` 的降级路径，极难定位。
> 最小脚本建议放 `scripts/` 下，命名 `milvus_smoke.py`。

---

## ⑧ 验收标准（可执行断言）

| # | 断言 | 验证方式 |
|---|---|---|
| 1 | 模块可在**未安装 pymilvus** 的环境下被 import（延迟导入生效） | `python -c "import rag_core.adapters.milvus_store"` |
| 2 | `MilvusVectorStore` 可从包出口导入 | `from rag_core.adapters import MilvusVectorStore` |
| 3 | 满足 `VectorStorePort` 协议（结构性检查） | `isinstance(MilvusVectorStore(...), VectorStorePort)` |
| 4 | `name` 属性形如 `milvus:server` | 直接断言 |
| 5 | 非法 `mode` 抛 `StoreUnavailableError` | `MilvusVectorStore(mode="bogus")` |
| 6 | `search` 返回的 `payload` 能 `ContractChunk.from_payload()` 还原 | 见 `1-5集成测试与迁移.md` |
| 7 | `_build_expr` 对含 `"` 的值不产生语法错 | 单测（**纯函数，无需 Milvus，可离线跑**） |
| 8 | 离线基线未退化 | `pytest -q` ≥ 140 passed |

> 💡 断言 7 是**唯一可以离线验证 expr 逻辑**的手段，**务必写**（`_build_expr` / `_escape` 是静态方法，不依赖连接）。

---

## ⑨ 测试要求

| 类型 | 文件 | 是否需 Milvus | 内容 |
|---|---|---|---|
| **单元（离线）** | `tests/test_milvus_store.py` | ❌ | `_escape` 转义正确性；`_build_expr` 各过滤组合的表达式；空 filters 返回 `""`；`_row_of` 的摊平与 `None→""` 映射；非法 mode 抛错 |
| **集成** | 同文件（`skipif`） | ✅ | 见 `1-5集成测试与迁移.md` 的 7 条断言 |

> ⚠️ **单元部分必须不依赖 Milvus**（因为构造 `MilvusVectorStore` 会建连）。
> 做法：**把 `_escape` / `_build_expr` / `_row_of` 做成 `@staticmethod`**（骨架已如此设计），
> 测试直接通过类调用，不实例化。

---

## ⑩ 避坑清单

| # | 坑 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | 试图把 `payload` 只存成 JSON 字段，过滤直接查嵌套键 | 过滤极慢或直接报错 | **必须冗余 6 个顶层标量列** |
| 2 | expr 里直接拼原始字符串 | 值含 `"` 时语法错；理论上有注入风险 | **一律过 `_escape()`** |
| 3 | `create_collection` 后忘记 `load` | `search` 报「collection not loaded」 | **传 `index_params` 给 `create_collection`**（自动 load） |
| 4 | 把 `distance` 当"距离"取小值优先排序 | **排序完全反了**（COSINE 下它是相似度） | 直接按返回值降序使用，与 Qdrant 一致 |
| 5 | 在模块顶层 `from pymilvus import MilvusClient` | 未装 pymilvus 时**整个 adapters 包都 import 失败**，默认离线路径被破坏 | **延迟导入**（骨架已如此） |
| 6 | 顺手删掉 `QdrantVectorStore` | 失去双跑校验基准与回退路径 | **保留**，两者并存 |
| 7 | 改 `_SCALAR_FIELDS` 字段名但与 Qdrant 侧不一致 | 同一 `RetrievalFilters` 在两库过滤出不同结果 | 两处字段名必须严格一致 |
| 8 | 认为 `upsert` 需要先 `delete` | 多余操作 | Milvus `upsert` 按主键覆盖，**天然幂等** |
| 9 | 在 Windows 上用 `mode="lite"` | 大概率不工作 | 用 `server` + Docker（见 `1-4Milvus容器编排.md`） |
| 10 | 给 `ensure_collection` 传错 `dim` | 集合与向量维度不符，写入报错 | 维度只从 `RagSettings.vector_dim`（1024）来，**不要硬编码第二份** |
| 11 | 在 `adapters/__init__.py` 顶层急切 `import MilvusVectorStore` | 未装 `pymilvus` 时**整个 `adapters` 包** import 失败，离线默认路径被破坏（与坑 5 同源，但在包出口侧） | 走既有 **PEP 562 `__getattr__` 懒导出**（见 §④ 末尾） |

---

## ⑪ 落地记录与实测纠偏（**2026-09-22**，T-A1 收口）

> 本节按 `0.§0.11.2` 规则 3 追加（**修正低层、不删原文**）：以下是**在目标版本上实测后**
> 与 §⑤ / §⑥ / §⑦ / §⑧ 原文**不一致**的地方。原文保留，以本节为准。

### ⑪.1 交付物（与 §④ 的差异：**多一个 pyproject**）

| 文件 | 动作 | 说明 |
|---|---|---|
| `packages/rag-core/rag_core/adapters/milvus_store.py` | **新建** | 与 §④ 一致 |
| `packages/rag-core/rag_core/adapters/__init__.py` | 修改 | 按 §④ 走 **PEP 562 懒导出**三处（`TYPE_CHECKING` / `__all__` / `__getattr__`） |
| `packages/rag-core/tests/test_milvus_store.py` | **新建** | 离线 17 + 集成 4（skipif） |
| `packages/rag-core/pyproject.toml` | 修改 | §④ 未列、但**组件卡 `1.C5` §④ 列为落点**：新增 extra `milvus = ["pymilvus>=2.4,<4.0"]`，并注明「只走 `CG_RAG_VECTOR_STORE=milvus` 显式启用，严禁成为默认值」 |

> ⚠️ **`1-3配置与工厂接线.md` §⑥ 与本文件 §④ 的冲突已裁决**：`1-3` 写「`adapters/__init__.py`
> 顶层急切 `import milvus_store` 是安全的」，本文件 §④（+ §⑩ 坑 11）**禁止**顶层急切导入。
> 采用**本文件**的 PEP 562 懒导出：它**同时满足** `1-3 §⑨` 断言 6
> （`from rag_core.adapters import MilvusVectorStore` 成功 —— PEP 562 对 `from … import …` 同样生效），
> 且不会在 `pymilvus` 缺包时把整个 `adapters` 包的出口绑死。
> **`1-3 §⑥` 的片段请在 T-A2 落地时按此修正。**
> ✅ **已于 2026-09-23（T-A2 / `0-C1`）核销**：`1-3 §⑥` 已追加纠偏块（顶层急切 import 作废），
> 且 T-A2 **未改** `adapters/__init__.py`（PEP 562 懒导出由 T-A1 落地）；
> 核销证据：`pytest packages/rag-core/tests/test_milvus_store.py -k "package_export or no_top_level"` → **2 passed**。

### ⑪.2 §⑦「必须实测确认的 6 个点」逐条结果（pymilvus **3.0.0** + milvus-lite **3.0**）

| # | 结论 | 实测证据 | 对代码的影响 |
|---|---|---|---|
| 1 | ✅ 参数名确认 | `MilvusClient.__init__(uri, user, password, db_name, token, timeout, **kwargs)`（`inspect.signature`）；**超时参数名是 `timeout` 而非 `timeout_s`** | §⑥ 骨架把 `timeout_s` 丢了（构造时未传）→ 已补 `timeout=timeout_s`，否则 T-A2 的 `CG_RAG_MILVUS_TIMEOUT_S` 会是**死配置** |
| 2 | ⚠️ 返回结构**与骨架预期不同** | `client.delete(filter=…)` 实测返回 **`list[str]`**（lite 返回主键）；pymilvus 源码在服务端不回主键时返回 `OmitZeroDict({"delete_count":…, "cost":…})`（**零值被省略**） | `delete_by_doc()` 加 `list/tuple` 分支（骨架只处理 dict → 会恒返回 0） |
| 3 | ✅ 可取到 JSON 字段 | 命中为 `dict`（`Hit(dict)`）：`{"chunk_id": …, "distance": …, "entity": {"payload": {…}}}` | — |
| 4 | 🔴 **与 §⑤ 原文相反** | `metric_type=COSINE` 时 `distance = 1 - cos`：同向量自检索 **0.0**、正交 **1.0**、反向 **2.0**，结果按该值**升序**；对照实验：`L2` 返回真实距离、`IP` 返回点积（越大越好） | 新增 `_score_from_distance()`：`score = 1 - distance`，把口径拉回契约的「余弦相似度、越大越相似」，与 Qdrant/InMemory 一致。**不修就会全库反向排序** |
| 5 | 🔴 **不支持标量索引** | `add_index(index_type="INVERTED")` 不传 `metric_type` → `create_index missing required 'metric_type' param`；传了 → `field 'doc_id' has type VARCHAR; create_index only supports vector fields`。即**带标量索引的 `create_collection` 整体失败** | 按 §⑦-5「不支持则去掉该循环」→ `ensure_collection()` **不建标量索引**（expr 过滤仍正确，仅失去过滤加速） |
| 6 | ✅ 64 安全 | uuid5 = 36 字符，`max_length=64` 写入通过；`_MAX_LEN` 其余字段与 `ContractChunk.max_length` 逐项核对 | 无需调整 |

### ⑪.3 §⑤ / §⑥ 的其余必要偏离

| # | 原文 | 实测后 | 理由 |
|---|---|---|---|
| 1 | §⑤「`distance` 即相似度」 | 见 ⑪.2-#4，反向映射 | 契约 `VectorHit.score`「越大越相似」 |
| 2 | §⑥ `__init__` 里 `uri: str = "http://localhost:19530"`（卡片 §⑤ 写 `uri=""`） | 采用 §⑥ 的默认值 | 与 `1-3 §⑤` 的 `milvus_uri` 默认值一致；`uri=""` 会构造必败 |
| 3 | §⑥ 骨架 `self._client = self._build_client(...)` 不包异常 | `_build_client` 内 **try/except → `StoreUnavailableError`** | 实测 `MilvusClient` **构造即建连**，服务不可达会抛裸 `MilvusException`；§⑥ docstring 已承诺 `Raises: StoreUnavailableError` |
| 4 | §⑥ `create_collection` 后即返回（`has_collection` 命中则直接 `return`） | 「已存在」分支补 `load_collection()` | 实测**跨进程**集合处于 `released`，`search` 报 `code=101 Collection … is in state 'released'`；`load` 幂等（连调 2 次 OK） |
| 5 | §⑥ `def _build_expr(self, …)`（实例方法） | 改为 `@staticmethod` | §⑨ 明确要求「`_escape` / `_build_expr` / `_row_of` 做成 `@staticmethod`，测试直接通过类调用、**不实例化**」（骨架自身不符，按 §⑨ 执行） |
| 6 | §⑥ 无命名字段常量 | 新增 `_PK_FIELD = "chunk_id"` | 实测命中字典的键是**主键字段名**（`search_result.py:151` 用 `pk_name`）而非 `id`：schema 写入与命中读取必须共用同一常量 |

### ⑪.4 §⑧ 断言与 §⑨ 测试要求的落地情况

| §⑧ 断言 | 结果 |
|---|---|
| 1 未装 pymilvus 可 import | ✅ `sys.modules['pymilvus']=None` 后 `import rag_core` + `build_service_from_settings()` → `offline ok -> RAGService` |
| 2 包出口可导入 | ✅ `from rag_core.adapters import MilvusVectorStore`（PEP 562） |
| 3 满足 `VectorStorePort` | ✅ `test_implements_vector_store_port`（注入假 `pymilvus`；**注意** `MilvusVectorStore()` 会真建连，无 server 时按设计抛 `StoreUnavailableError`） |
| 4 `name` 形如 `milvus:server` | ✅ `test_name_reflects_mode` |
| 5 非法 mode 抛错 | ✅ `test_invalid_mode_raises` |
| 6 payload 可 `from_payload()` 还原 | ✅ 集成用例断言 `hits[0].payload == chunk.to_payload()`（等价于 `from_payload` 零改动可用） |
| 7 `_build_expr` 对含 `"` 的值不产生语法错 | ✅ 离线 `test_build_expr_escapes_quoted_values` + 集成 `test_search_respects_filters`（真机 expr 不报错） |
| 8 离线基线未退化 | ✅ 全量 `pytest` → 干净时 **334 passed / 6 skipped / 0 failed**（exit 0，出现 2 次）。基线核对：改动前收集 **319**（= `317 passed + 2 skipped`，与 `AGENTS.md §7` 一致）；改动后收集 **340**（+21，全部在 `rag-core`）→ 现有用例**无一退化**。⚠️ 唯一不稳定用例：`packages/doc-processor/tests/test_ocr_engine.py::test_ocr_pdf_page_uses_render_dpi`（真实 RapidOCR，页级超时固定 30 s，而单页 A4@200dpi 本机实测 **47.3 s**）——**改动前的基线首跑即失败**，与本次改动无因果关系（`doc_processor` 不 import `rag_core`），属模块 3 / T-C2 边界，未处置 |

### ⑪.5 集成用例实跑方式（**无 Docker 也能验**）

本机 Docker daemon 未起，但有 `milvus-lite 3.0`（`pymilvus` 3.0.0 自带 `.db` URI 支持）——
把 URI 指向一个本地 `.db` 文件即可跑通全部 4 条集成断言（**T-A4 的真机联调仍按 `1-4` 起 server**）：

```powershell
$env:CG_RAG_TEST_MILVUS_URI = "$root\.tmp_t\milvus_it.db"    # 或 http://localhost:19530（T-A3 起服务后）
D:\aanacoda1\python.exe -m pytest packages/rag-core/tests/test_milvus_store.py -q
# 实测：21 passed（含 4 条集成）；Top-10 重合度 1.00
```

**未做项**：真机 `server` 模式联调（需 `1-3` 配置接线 + `1-4` 容器编排，属 T-A2 / T-A3 / T-A4）。
