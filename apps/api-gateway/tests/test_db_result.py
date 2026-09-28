"""C5 结果仓储的 PostgreSQL 集成测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.db.models import AuditRisk
from app.db.repository.contract import ContractCreate, ContractRepository
from app.db.repository.result import (
    ElementRepository,
    ReportRepository,
    RiskRepository,
    SealRepository,
)
from app.generated import schemas as s
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
API_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = API_GATEWAY_ROOT / "alembic.ini"


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


async def _create_schema(dsn: str, schema: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def _drop_schema(dsn: str, schema: str) -> None:
    engine = create_async_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        await engine.dispose()


def _schema_engine(dsn: str, schema: str) -> AsyncEngine:
    return create_async_engine(
        dsn,
        connect_args={"server_settings": {"search_path": schema}},
    )


async def _migrate(dsn: str, schema: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    engine = _schema_engine(dsn, schema)
    try:
        async with engine.connect() as connection:

            def run_migration(sync_connection: Connection) -> None:
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(run_migration)
    finally:
        await engine.dispose()


def _bbox() -> s.BBox:
    return s.BBox(
        page=1,
        x=72.0,
        y=720.0,
        width=300.0,
        height=40.0,
        coordinateSystem=s.CoordinateSystem.pdf_points,
    )


def _citation() -> s.PreciseCitation:
    return s.PreciseCitation(
        sourceType=s.CitationSourceType.law_article,
        sourceTitle="《民法典》第五百八十五条",
        contentChunk="约定的违约金过分高于造成的损失的，可以适当减少。",
        relevanceScore=0.93,
    )


def _chunk(text_value: str) -> ContractChunk:
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id("contract-c5", "3.2"),
        doc_id="contract-c5",
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id="3.2",
        section_path="第三章/3.2",
        seq_in_doc=1,
        text=text_value,
        text_hash=ContractChunk.compute_text_hash(text_value),
        char_count=len(text_value),
        token_count=5,
        contract_type=ContractType.PURCHASE,
        category="PAYMENT",
        tags=["付款", "违约金"],
        embedding_model="hash-embedder",
    )


def _risk() -> s.EnhancedRiskItem:
    return s.EnhancedRiskItem(
        id="risk-c5-1",
        clauseId="3.2",
        category=s.RiskCategory.penalty_too_high,
        level=s.RiskLevel.high,
        title="违约金比例过高",
        originalText="逾期付款按合同总额 30% 支付违约金。",
        bbox=_bbox(),
        suggestion="将违约金调减至合理范围。",
        legalBasis="《民法典》第五百八十五条",
        reasoningSteps=[
            s.ReasoningStep(
                stepNumber=1,
                thought="识别违约金比例。",
                evidenceText="合同总额 30% 支付违约金。",
            )
        ],
        citations=[_citation()],
    )


def _conflict() -> s.CrossClauseConflict:
    return s.CrossClauseConflict(
        id="conflict-c5-1",
        sourceClauseId="3.2",
        targetClauseId="6.1",
        conflictType=s.ConflictType.payment_contradiction,
        description="付款违约金与逾期利息口径冲突。",
        level=s.RiskLevel.medium,
        suggestion="统一付款违约责任口径。",
        citations=[_citation()],
    )


def _seal() -> s.SealSignatureDetection:
    return s.SealSignatureDetection(
        id="seal-c5-1",
        type=s.SealType.official_seal,
        status=s.SealStatus.valid,
        bbox=_bbox(),
        page=1,
        extractedText="北京示例科技有限公司",
        isMatchedWithParties=True,
        complianceNote="公章清晰且与合同主体一致。",
        confidence=0.97,
    )


async def _exercise_result_repositories(dsn: str, schema: str) -> None:
    engine = _schema_engine(dsn, schema)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            contract = await ContractRepository(session).create(
                ContractCreate(
                    filename="结果测试合同.docx",
                    file_size=4096,
                    file_hash="d" * 64,
                    storage_path="contracts/result-test.docx",
                    contract_type=ContractType.PURCHASE,
                )
            )
            await session.commit()

            elements = ElementRepository(session)
            original_chunk = _chunk("原付款条款正文。")
            await elements.bulk_upsert(contract.id, [original_chunk])
            await session.commit()

            loaded_chunks = await elements.list_by_contract(contract.id)
            assert loaded_chunks == [original_chunk]

            updated_chunk = _chunk("更新后的付款条款正文。")
            await elements.bulk_upsert(contract.id, [updated_chunk, updated_chunk])
            await session.commit()

            upserted_chunks = await elements.list_by_contract(contract.id)
            assert len(upserted_chunks) == 1
            assert upserted_chunks[0].text == updated_chunk.text

            risks = RiskRepository(session)
            risk = _risk()
            conflict = _conflict()
            await risks.bulk_upsert(contract.id, [risk, conflict])
            await session.commit()

            loaded_risks = await risks.list_by_contract(contract.id)
            assert loaded_risks == [risk]

            updated_risk = risk.model_copy(
                update={"suggestion": "更新后的风险处置建议。"}
            )
            await risks.bulk_upsert(contract.id, [updated_risk, updated_risk])
            await session.commit()
            assert await risks.list_by_contract(contract.id) == [updated_risk]

            updated_conflict = conflict.model_copy(
                update={"suggestion": "更新后的矛盾处置建议。"}
            )
            await risks.bulk_upsert(contract.id, [updated_conflict])
            await session.commit()

            high_risks = await risks.list_by_contract(contract.id, s.RiskLevel.high)
            assert high_risks == [updated_risk]
            assert await risks.list_by_contract(contract.id, s.RiskLevel.low) == []

            conflict_row = await session.scalar(
                select(AuditRisk).where(AuditRisk.id == conflict.id)
            )
            assert conflict_row is not None
            assert conflict_row.conflict_type is s.ConflictType.payment_contradiction
            assert conflict_row.suggestion == updated_conflict.suggestion

            seals = SealRepository(session)
            seal = _seal()
            await seals.bulk_upsert(contract.id, [seal, seal])
            await session.commit()
            assert await seals.list_by_contract(contract.id) == [seal]

            updated_seal = seal.model_copy(
                update={
                    "status": s.SealStatus.suspicious_overlap,
                    "confidence": 0.61,
                }
            )
            await seals.bulk_upsert(contract.id, [updated_seal])
            await session.commit()
            assert await seals.list_by_contract(contract.id) == [updated_seal]

            reports = ReportRepository(session)
            report = await reports.create(
                contract.id,
                "DOCX",
                "reports/result-test.docx",
            )
            await session.commit()

            loaded_report = await reports.get(contract.id)
            assert loaded_report == report
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=("未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 结果仓储集成测试（保持离线基线）"),
)
def test_result_repositories_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    schema = f"cg_c5_{uuid4().hex}"

    _run(_create_schema(POSTGRES_DSN, schema))
    try:
        _run(_migrate(POSTGRES_DSN, schema))
        _run(_exercise_result_repositories(POSTGRES_DSN, schema))
    finally:
        _run(_drop_schema(POSTGRES_DSN, schema))
