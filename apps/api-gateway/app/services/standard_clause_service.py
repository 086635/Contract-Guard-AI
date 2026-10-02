"""C70 标准条款库服务：PG 人工维护源 + 现有 RAG 检索索引。

设计要点：
1. 检索不新增第二套实现：PG 表只是人工维护源，检索命中仍由 `RagTool` →
   `RAGService.retrieve()` 唯一入口产出；本服务只把条款同步进**同一个**
   `contract_clauses` 向量集合（不新建第二个 collection）。
2. 删除 = 软停用：PG 置 `is_enabled=False`，同时把该点从索引删除，
   保证“停用后不再命中生产检索”。
3. 索引写入幂等：`chunk_id` 由 `ContractChunk.make_chunk_id(doc_id, section_id, 0)`
   确定性生成，重复 upsert 不产生孤儿向量；`rebuild_index` 可随时全量重建。

对应：`文件集合/codex/4.C70标准条款库数据与服务.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository.standard_clause import (
    StandardClauseCreate,
    StandardClausePatch,
    StandardClauseRecord,
    StandardClauseRepository,
)
from app.generated.schemas import ContractType
from cg_observability import get_logger, log_event
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, SourceType
from rag_contracts.enums import ContractType as RagContractType
from rag_contracts.ports import EmbedderPort, VectorRecord, VectorStorePort
from rag_core.config import RagSettings
from rag_core.factory import build_service_from_settings

_LOG = get_logger("api_gateway.services.standard_clause")


class StandardClauseValidationError(ValueError):
    """参数问题：PATCH 未提供任何可更新字段。"""


class StandardClauseIndexError(RuntimeError):
    """索引写入失败（向量库/embedding 不可用）；元数据事务已回滚。"""


class StandardClauseIndexer:
    """把标准条款投影为 `ContractChunk` 并写入现有 RAG 向量集合。"""

    def __init__(self, embedder: EmbedderPort, vector_store: VectorStorePort) -> None:
        self._embedder = embedder
        self._vector_store = vector_store

    @classmethod
    def from_settings(
        cls, settings: RagSettings | None = None
    ) -> StandardClauseIndexer:
        """用 RAG 唯一工厂装配（生产 qdrant / 离线 inmemory 同一入口）。"""
        service = build_service_from_settings(settings)
        return cls(service.embedder, service.vector_store)

    @staticmethod
    def doc_id(clause_id: UUID) -> str:
        """向量库 `doc_id`：`std:<id>` 独立命名空间，避免与合同/法规冲突。"""
        return f"std:{clause_id}"

    @staticmethod
    def section_id(clause_id: UUID) -> str:
        """切片 `section_id`：稳定且可读，供引用溯源展示。"""
        return f"std-{clause_id}"

    @classmethod
    def chunk_id(cls, clause_id: UUID) -> str:
        """确定性 chunk_id（重复重建幂等，不留孤儿向量）。"""
        return ContractChunk.make_chunk_id(
            cls.doc_id(clause_id), cls.section_id(clause_id), 0
        )

    def build_chunk(self, record: StandardClauseRecord) -> ContractChunk:
        """把标准条款投影为 `SourceType.STANDARD_CLAUSE` 的 `ContractChunk`。"""
        text = record.text
        return ContractChunk(
            chunk_id=self.chunk_id(record.id),
            doc_id=self.doc_id(record.id),
            source_type=SourceType.STANDARD_CLAUSE,
            chunk_type=ChunkType.CLAUSE,
            section_id=self.section_id(record.id),
            section_path=record.title,
            seq_in_doc=0,
            title=record.title,
            text=text,
            text_hash=ContractChunk.compute_text_hash(text),
            char_count=len(text),
            token_count=max(1, round(len(text) / 1.5)),
            level=3,
            contract_type=RagContractType(record.contract_type.value),
            embedding_model=self._embedder.model_name,
            embedding_dim=self._embedder.dim,
        )

    def upsert(self, records: Sequence[StandardClauseRecord]) -> int:
        """批量写入；返回成功写入条数（幂等 upsert）。"""
        if not records:
            return 0
        chunks = [self.build_chunk(record) for record in records]
        self._vector_store.ensure_collection(self._embedder.dim)
        vectors = self._embedder.embed_batch([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise StandardClauseIndexError("embedding 返回条数与条款数不一致")
        return self._vector_store.upsert(
            [
                VectorRecord(chunk=chunk, vector=vector)
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        )

    def remove(self, clause_id: UUID) -> int:
        """从索引删除该条款的全部点（停用/软删除）。"""
        return self._vector_store.delete_by_doc(self.doc_id(clause_id))

    def health(self) -> bool:
        """索引健康检查（运营入口复用 `/health/ready` 的 qdrant 探针）。"""
        try:
            return bool(self._vector_store.health())
        except Exception:  # noqa: BLE001 - 健康检查必须给出布尔结论
            return False


class StandardClauseService:
    """标准条款 CRUD 与索引重建（ADMIN 运营面）。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: StandardClauseRepository | None = None,
        indexer: StandardClauseIndexer | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or StandardClauseRepository(session)
        self._indexer = indexer

    @property
    def indexer(self) -> StandardClauseIndexer:
        """惰性装配索引写入器（首次写库时才加载 RAG/向量库依赖）。"""
        if self._indexer is None:
            self._indexer = StandardClauseIndexer.from_settings()
        return self._indexer

    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        query: str | None = None,
        contract_type: ContractType | None = None,
        is_enabled: bool | None = None,
    ) -> tuple[list[StandardClauseRecord], int]:
        """分页查询标准条款（ADMIN 可见停用条目）。"""
        return await self._repo.list(
            page,
            page_size,
            query=query,
            contract_type=contract_type,
            is_enabled=is_enabled,
        )

    async def create(self, payload: StandardClauseCreate) -> StandardClauseRecord:
        """新建条款并同步索引；索引失败则整体回滚（可重试）。"""
        record = await self._repo.create(payload)
        await self._sync(record)
        await self._session.commit()
        log_event(
            _LOG,
            "standard_clause.created",
            clause_id=str(record.id),
            contract_type=record.contract_type.value,
        )
        return record

    async def update(
        self, clause_id: UUID, patch: StandardClausePatch
    ) -> StandardClauseRecord:
        """更新/启停条款并同步索引（乐观锁版本校验）。"""
        if not any(
            (
                patch.title is not None,
                patch.contract_type is not None,
                patch.text is not None,
                patch.is_enabled is not None,
            )
        ):
            raise StandardClauseValidationError("至少提供一个可更新字段")
        record = await self._repo.update(clause_id, patch)
        await self._sync(record)
        await self._session.commit()
        log_event(
            _LOG,
            "standard_clause.updated",
            clause_id=str(record.id),
            is_enabled=record.is_enabled,
            version=record.version,
        )
        return record

    async def rebuild_index(self) -> dict[str, Any]:
        """全量重建索引：启用条目 upsert + 停用条目删除（幂等、可重试）。"""
        records = await self._repo.list_all()
        enabled = [record for record in records if record.is_enabled]
        disabled = [record for record in records if not record.is_enabled]
        try:
            indexed = self.indexer.upsert(enabled)
            for record in disabled:
                self.indexer.remove(record.id)
            healthy = self.indexer.health()
        except Exception as exc:
            raise StandardClauseIndexError(str(exc)) from exc
        result: dict[str, Any] = {
            "total": len(records),
            "enabled": len(enabled),
            "indexed": indexed,
            "removed": len(disabled),
            "indexHealthy": healthy,
        }
        log_event(_LOG, "standard_clause.index_rebuilt", **result)
        return result

    async def _sync(self, record: StandardClauseRecord) -> None:
        """把单条条款同步进检索索引；失败回滚元数据并抛 503 语义异常。"""
        try:
            if record.is_enabled:
                self.indexer.upsert([record])
            else:
                self.indexer.remove(record.id)
        except Exception as exc:
            await self._session.rollback()
            raise StandardClauseIndexError(str(exc)) from exc


__all__ = [
    "StandardClauseIndexError",
    "StandardClauseIndexer",
    "StandardClauseService",
    "StandardClauseValidationError",
]
