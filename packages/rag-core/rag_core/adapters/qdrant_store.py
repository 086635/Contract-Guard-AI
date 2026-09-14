"""Qdrant 向量库适配器：实现 `rag_contracts.ports.VectorStorePort`。

支持三种模式（`CG_RAG_QDRANT_MODE`）：
- `server`       —— 连 docker-compose 里的 Qdrant 服务（:6333）
- `local_memory` —— `QdrantClient(":memory:")`，无需起服务（Phase 1 推荐）
- `local_path`   —— `QdrantClient(path=...)`，落本地磁盘，进程重启数据仍在

⚠️ 依赖 `qdrant-client` 仅在**本模块内**导入：
默认路径（`InMemoryVectorStore`）不会 import 本模块，因此离线环境零影响。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag_contracts.errors import StoreUnavailableError
from rag_contracts.ports import VectorHit, VectorRecord
from rag_contracts.query import RetrievalFilters

# 需要建 keyword payload 索引的字段（供过滤加速）
KEYWORD_INDEX_FIELDS: tuple[str, ...] = (
    "source_type",
    "chunk_type",
    "contract_type",
    "doc_id",
    "category",
    "law_code",
)


class QdrantVectorStore:
    """Qdrant 适配器。

    Attributes:
        name: 适配器标识，写入日志 `store` 字段。
    """

    def __init__(
        self,
        *,
        mode: str = "local_memory",
        url: str = "http://localhost:6333",
        path: str = "./data/qdrant",
        api_key: str | None = None,
        collection: str = "contract_clauses",
        timeout_s: float = 3.0,
    ) -> None:
        """初始化客户端（延迟到首次使用才真正建连）。

        Args:
            mode: server / local_memory / local_path。
            url: server 模式的服务地址。
            path: local_path 模式的数据目录。
            api_key: Qdrant Cloud API Key。
            collection: 集合名。
            timeout_s: 请求超时（秒）。

        Raises:
            StoreUnavailableError: qdrant-client 未安装或参数非法。
        """
        self._mode = mode
        self._collection = collection
        self.name: str = f"qdrant:{mode}"
        self._client: Any = self._build_client(mode, url, path, api_key, timeout_s)

    @staticmethod
    def _build_client(
        mode: str, url: str, path: str, api_key: str | None, timeout_s: float
    ) -> Any:
        """按模式构造 QdrantClient。

        Args:
            mode: 运行模式。
            url: 服务地址。
            path: 本地目录。
            api_key: API Key。
            timeout_s: 超时秒数。

        Returns:
            QdrantClient 实例。

        Raises:
            StoreUnavailableError: 依赖缺失或 mode 非法。
        """
        try:
            from qdrant_client import QdrantClient  # 延迟导入，避免污染默认路径
        except ImportError as exc:  # pragma: no cover - 仅在缺包环境触发
            raise StoreUnavailableError(
                "未安装 qdrant-client，请改用 CG_RAG_VECTOR_STORE=inmemory 或安装该包"
            ) from exc

        # 上层约定 timeout_s 为 float（秒），而 QdrantClient(timeout=) 期望 int | None，
        # 此处显式取整，避免 mypy arg-type 报错。
        timeout = int(timeout_s)
        if mode == "local_memory":
            return QdrantClient(":memory:", timeout=timeout)
        if mode == "local_path":
            return QdrantClient(path=path, timeout=timeout)
        if mode == "server":
            return QdrantClient(url=url, api_key=api_key, timeout=timeout)
        raise StoreUnavailableError(f"不支持的 qdrant mode：{mode}")

    # ---------------- 内部工具 ----------------
    def _build_filter(self, filters: RetrievalFilters | None) -> Any | None:
        """把 RetrievalFilters 翻译成 Qdrant Filter。

        Args:
            filters: 契约层过滤条件。

        Returns:
            qdrant_client.models.Filter 或 None。
        """
        if filters is None:
            return None
        from qdrant_client import models

        conditions: list[Any] = []
        if filters.source_types:
            conditions.append(
                models.FieldCondition(
                    key="source_type",
                    match=models.MatchAny(any=[s.value for s in filters.source_types]),
                )
            )
        if filters.chunk_types:
            conditions.append(
                models.FieldCondition(
                    key="chunk_type",
                    match=models.MatchAny(any=[c.value for c in filters.chunk_types]),
                )
            )
        if filters.contract_type:
            conditions.append(
                models.FieldCondition(
                    key="contract_type",
                    match=models.MatchValue(value=filters.contract_type.value),
                )
            )
        if filters.law_codes:
            conditions.append(
                models.FieldCondition(key="law_code", match=models.MatchAny(any=filters.law_codes))
            )
        if filters.doc_ids:
            conditions.append(
                models.FieldCondition(key="doc_id", match=models.MatchAny(any=filters.doc_ids))
            )
        if filters.categories:
            conditions.append(
                models.FieldCondition(key="category", match=models.MatchAny(any=filters.categories))
            )
        if not conditions:
            return None
        return models.Filter(must=conditions)

    # ---------------- VectorStorePort ----------------
    def ensure_collection(self, dim: int) -> None:
        """建集合 + payload keyword 索引（幂等）。

        Args:
            dim: 向量维度。
        """
        from qdrant_client import models

        exists = self._client.collection_exists(self._collection)
        if not exists:
            # HNSW 参数显式化（C3 决策）：不依赖 SDK 默认，避免随版本漂移导致
            # 召回率/延迟不可预期。m=16 每节点连接数；ef_construct=200 建索引
            # 时考察的候选数（越大越准、建索引越慢）。
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=models.VectorParams(
                    size=int(dim), distance=models.Distance.COSINE
                ),
                hnsw_config=models.HnswConfigDiff(m=16, ef_construct=200),
            )
        if self._mode != "server":
            # 本地模式（:memory: / path）下 payload 索引无效，建了只会刷警告
            return
        for field in KEYWORD_INDEX_FIELDS:
            try:
                self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception:  # noqa: BLE001 - 索引已存在会抛错，幂等忽略
                continue

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """幂等写入：`point_id == chunk_id`。

        Args:
            records: （chunk, vector）序列。

        Returns:
            写入条数。

        Raises:
            StoreUnavailableError: 写入失败。
        """
        from qdrant_client import models

        if not records:
            return 0
        points = [
            models.PointStruct(
                id=record.chunk.chunk_id,
                vector=list(record.vector),
                payload=record.chunk.to_payload(),
            )
            for record in records
        ]
        try:
            self._client.upsert(collection_name=self._collection, points=points, wait=True)
        except Exception as exc:
            raise StoreUnavailableError(f"Qdrant upsert 失败：{exc}") from exc
        return len(points)

    def search(
        self,
        vector: Sequence[float],
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[VectorHit]:
        """稠密检索。

        Args:
            vector: 查询向量。
            top_k: 返回条数上限。
            filters: 过滤条件。

        Returns:
            VectorHit 列表（得分降序）。

        Raises:
            StoreUnavailableError: 检索失败。
        """
        try:
            response = self._client.query_points(
                collection_name=self._collection,
                query=list(vector),
                limit=max(1, int(top_k)),
                query_filter=self._build_filter(filters),
                with_payload=True,
            )
        except Exception as exc:
            raise StoreUnavailableError(f"Qdrant search 失败：{exc}") from exc

        points = getattr(response, "points", None) or []
        return [
            VectorHit(
                chunk_id=str(point.id),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in points
        ]

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除（先查 id 再删，Qdrant 不支持按 payload 直接删）。

        ⚠️ 已知限制（C8，暂不处理）：单次 scroll 上限 10_000 条。若某篇文档的
        切片数超过 1 万，超出部分不会被删除，将残留孤儿向量。合同场景单文档
        通常 < 500 切片，不会触发；待"整部法规批量导入"场景出现时再改为
        分页 scroll 循环（用 offset 游标翻页直到取尽）。

        Args:
            doc_id: 源文档标识。

        Returns:
            删除条数。
        """
        from qdrant_client import models

        selector = models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
        )
        try:
            points, _ = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=selector,
                limit=10_000,
                with_payload=False,
            )
        except Exception as exc:
            raise StoreUnavailableError(f"Qdrant scroll 失败：{exc}") from exc

        ids = [point.id for point in points]
        if not ids:
            return 0
        self._client.delete(
            collection_name=self._collection,
            points_selector=models.PointIdsList(points=ids),
            wait=True,
        )
        return len(ids)

    def health(self) -> bool:
        """健康检查。

        Returns:
            True 表示可用。
        """
        try:
            self._client.get_collections()
            return True
        except Exception:  # noqa: BLE001
            return False

    def count(self) -> int:
        """当前集合内点数。"""
        try:
            info = self._client.count(collection_name=self._collection, exact=True)
            return int(getattr(info, "count", 0))
        except Exception:  # noqa: BLE001 - 集合不存在时返回 0
            return 0


__all__ = ["KEYWORD_INDEX_FIELDS", "QdrantVectorStore"]
