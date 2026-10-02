"""交付模块 C5：结果表仓储。

设计要点：
1. 切片、风险/矛盾和报告分别按聚合边界提供异步仓储。
2. 批量写入使用 PostgreSQL 主键冲突更新，不逐条 select+insert。
3. 切片读取直接还原 `ContractChunk`，避免复制 28 字段契约。

对应：`文件集合/codex/4.C5结果DAL.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditReport, AuditRisk, ContractElement, SealDetection
from app.generated.schemas import (
    CrossClauseConflict,
    EnhancedRiskItem,
    RiskLevel,
    SealSignatureDetection,
    Status,
)
from rag_contracts.chunk import ContractChunk


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class ReportRecord(_PersistenceDto):
    """报告元数据持久化投影。"""

    id: UUID
    contract_id: UUID
    format: str
    storage_path: str
    pdf_storage_path: str | None = None
    download_count: int
    generated_at: datetime
    status: Status


class ElementRepository:
    """合同切片仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(
        self,
        contract_id: UUID,
        elements: Sequence[ContractChunk],
    ) -> None:
        """按 `chunk_id` 批量 upsert 切片。"""
        if not elements:
            return

        unique_elements = {chunk.chunk_id: chunk for chunk in elements}
        rows = []
        for chunk in unique_elements.values():
            payload = chunk.model_dump()
            payload["contract_id"] = contract_id
            payload["vector_id"] = chunk.chunk_id
            rows.append(payload)

        statement = pg_insert(ContractElement).values(rows)
        update_columns = {
            column.name: getattr(statement.excluded, column.name)
            for column in ContractElement.__table__.columns
            if column.name not in {"chunk_id", "created_at"}
        }
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[ContractElement.chunk_id],
                set_=update_columns,
            )
        )

    async def list_by_contract(
        self,
        contract_id: UUID,
    ) -> list[ContractChunk]:
        """按合同读取切片并还原契约对象。"""
        statement = (
            select(ContractElement)
            .where(ContractElement.contract_id == contract_id)
            .order_by(ContractElement.seq_in_doc.asc(), ContractElement.chunk_id.asc())
        )
        records = (await self._session.scalars(statement)).all()
        return [
            ContractChunk.model_validate(
                {
                    field_name: getattr(record, field_name)
                    for field_name in ContractChunk.model_fields
                }
            )
            for record in records
        ]


class RiskRepository:
    """风险与矛盾结果仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(
        self,
        contract_id: UUID,
        risks: Sequence[EnhancedRiskItem | CrossClauseConflict],
    ) -> None:
        """按结果 ID 批量 upsert 风险与矛盾。"""
        if not risks:
            return

        unique_results = {result.id: result for result in risks}
        rows = [
            self._row_from_result(contract_id, result)
            for result in unique_results.values()
        ]
        statement = pg_insert(AuditRisk).values(rows)
        update_columns = {
            column.name: getattr(statement.excluded, column.name)
            for column in AuditRisk.__table__.columns
            if column.name not in {"id", "contract_id"}
        }
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[AuditRisk.id],
                set_=update_columns,
            )
        )

    async def list_by_contract(
        self,
        contract_id: UUID,
        level: RiskLevel | None = None,
    ) -> list[EnhancedRiskItem]:
        """按合同与可选等级读取风险结果。"""
        statement = (
            select(AuditRisk)
            .where(
                AuditRisk.contract_id == contract_id,
                AuditRisk.category.is_not(None),
            )
            .order_by(AuditRisk.id.asc())
        )
        if level is not None:
            statement = statement.where(AuditRisk.level == level)

        records = (await self._session.scalars(statement)).all()
        return [self._to_risk_dto(record) for record in records]

    async def list_conflicts_by_contract(
        self,
        contract_id: UUID,
    ) -> list[CrossClauseConflict]:
        """按合同读取跨条款矛盾。"""
        statement = (
            select(AuditRisk)
            .where(
                AuditRisk.contract_id == contract_id,
                AuditRisk.conflict_type.is_not(None),
            )
            .order_by(AuditRisk.id.asc())
        )
        records = (await self._session.scalars(statement)).all()
        return [self._to_conflict_dto(record) for record in records]

    @staticmethod
    def _row_from_result(
        contract_id: UUID,
        result: EnhancedRiskItem | CrossClauseConflict,
    ) -> dict[str, object]:
        data = result.model_dump(mode="json", by_alias=True)
        row: dict[str, object] = {
            "id": data["id"],
            "contract_id": contract_id,
            "level": data["level"],
            "suggestion": data["suggestion"],
            "citations": data["citations"],
            "clause_id": None,
            "category": None,
            "title": None,
            "original_text": None,
            "bbox": None,
            "legal_basis": None,
            "reasoning_steps": [],
            "source_clause_id": None,
            "target_clause_id": None,
            "conflict_type": None,
            "description": None,
        }
        if isinstance(result, EnhancedRiskItem):
            row.update(
                {
                    "clause_id": data["clauseId"],
                    "category": data["category"],
                    "title": data["title"],
                    "original_text": data["originalText"],
                    "bbox": data["bbox"],
                    "legal_basis": data["legalBasis"],
                    "reasoning_steps": data["reasoningSteps"],
                }
            )
        else:
            row.update(
                {
                    "source_clause_id": data["sourceClauseId"],
                    "target_clause_id": data["targetClauseId"],
                    "conflict_type": data["conflictType"],
                    "description": data["description"],
                }
            )
        return row

    @staticmethod
    def _to_risk_dto(record: AuditRisk) -> EnhancedRiskItem:
        return EnhancedRiskItem.model_validate(
            {
                "id": record.id,
                "clauseId": record.clause_id,
                "category": record.category,
                "level": record.level,
                "title": record.title,
                "originalText": record.original_text,
                "bbox": record.bbox,
                "suggestion": record.suggestion,
                "legalBasis": record.legal_basis,
                "reasoningSteps": record.reasoning_steps,
                "citations": record.citations,
            }
        )

    @staticmethod
    def _to_conflict_dto(record: AuditRisk) -> CrossClauseConflict:
        return CrossClauseConflict.model_validate(
            {
                "id": record.id,
                "sourceClauseId": record.source_clause_id,
                "targetClauseId": record.target_clause_id,
                "conflictType": record.conflict_type,
                "description": record.description,
                "level": record.level,
                "suggestion": record.suggestion,
                "citations": record.citations,
            }
        )


class SealRepository:
    """印章与签名检测结果仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_upsert(
        self,
        contract_id: UUID,
        seals: Sequence[SealSignatureDetection],
    ) -> None:
        """按检测 ID 批量 upsert 印章结果。"""
        if not seals:
            return

        unique_seals = {seal.id: seal for seal in seals}
        rows = [
            self._row_from_result(contract_id, seal) for seal in unique_seals.values()
        ]
        statement = pg_insert(SealDetection).values(rows)
        update_columns = {
            column.name: getattr(statement.excluded, column.name)
            for column in SealDetection.__table__.columns
            if column.name not in {"id", "contract_id"}
        }
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[SealDetection.id],
                set_=update_columns,
            )
        )

    async def list_by_contract(
        self,
        contract_id: UUID,
    ) -> list[SealSignatureDetection]:
        """按合同读取印章与签名检测结果。"""
        statement = (
            select(SealDetection)
            .where(SealDetection.contract_id == contract_id)
            .order_by(SealDetection.page.asc(), SealDetection.id.asc())
        )
        records = (await self._session.scalars(statement)).all()
        return [self._to_seal_dto(record) for record in records]

    @staticmethod
    def _row_from_result(
        contract_id: UUID,
        seal: SealSignatureDetection,
    ) -> dict[str, object]:
        data = seal.model_dump(mode="json", by_alias=True)
        return {
            "id": data["id"],
            "contract_id": contract_id,
            "type": data["type"],
            "status": data["status"],
            "bbox": data["bbox"],
            "page": data["page"],
            "extracted_text": data["extractedText"],
            "is_matched_with_parties": data["isMatchedWithParties"],
            "compliance_note": data["complianceNote"],
            "confidence": data["confidence"],
        }

    @staticmethod
    def _to_seal_dto(record: SealDetection) -> SealSignatureDetection:
        return SealSignatureDetection.model_validate(
            {
                "id": record.id,
                "type": record.type,
                "status": record.status,
                "bbox": record.bbox,
                "page": record.page,
                "extractedText": record.extracted_text,
                "isMatchedWithParties": record.is_matched_with_parties,
                "complianceNote": record.compliance_note,
                "confidence": record.confidence,
            }
        )


class ReportRepository:
    """报告元数据仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        contract_id: UUID,
        format: str,
        storage_path: str,
        *,
        pdf_storage_path: str | None = None,
    ) -> ReportRecord:
        """创建并 flush 报告记录。"""
        record = AuditReport(
            contract_id=contract_id,
            format=format,
            storage_path=storage_path,
            pdf_storage_path=pdf_storage_path or None,
            download_count=0,
            status=Status.done,
        )
        self._session.add(record)
        await self._session.flush()
        return ReportRecord.model_validate(record)

    async def get(self, contract_id: UUID) -> ReportRecord | None:
        """读取合同最新报告。"""
        statement = (
            select(AuditReport)
            .where(AuditReport.contract_id == contract_id)
            .order_by(AuditReport.generated_at.desc(), AuditReport.id.asc())
            .limit(1)
        )
        record = await self._session.scalar(statement)
        if record is None:
            return None
        return ReportRecord.model_validate(record)


__all__ = [
    "ElementRepository",
    "ReportRecord",
    "ReportRepository",
    "RiskRepository",
    "SealRepository",
]
