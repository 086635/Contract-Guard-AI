"""C12 合同摘要与详情服务真实化测试。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)

from app.api.deps import get_service_context
from app.api.v1 import contracts as contracts_router
from app.db.repository.contract import ContractCreate, ContractRepository
from app.db.repository.result import (
    ElementRepository,
    ReportRepository,
    RiskRepository,
    SealRepository,
)
from app.generated import schemas as s
from rag_contracts.chunk import BBox as ChunkBBox
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


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
        contentChunk="违约金过高可以适当减少。",
        relevanceScore=0.9,
    )


def _chunk() -> ContractChunk:
    text = "乙方逾期付款的，应按合同总额 30% 支付违约金。"
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id("contract-c12", "3.2"),
        doc_id="contract-c12",
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id="3.2",
        section_path="第三章/3.2",
        seq_in_doc=1,
        title="付款违约金",
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=8,
        bbox=ChunkBBox(
            page=1,
            x=72.0,
            y=720.0,
            width=300.0,
            height=40.0,
        ),
        contract_type=ContractType.PURCHASE,
        category="PAYMENT",
    )


def _risk() -> s.EnhancedRiskItem:
    return s.EnhancedRiskItem(
        id="risk-c12",
        clauseId="3.2",
        category=s.RiskCategory.penalty_too_high,
        level=s.RiskLevel.high,
        title="违约金过高",
        originalText="合同总额 30% 违约金。",
        bbox=_bbox(),
        suggestion="调减违约金。",
        legalBasis="《民法典》第五百八十五条",
        reasoningSteps=[
            s.ReasoningStep(
                stepNumber=1,
                thought="识别过高比例。",
                evidenceText="合同总额 30%。",
            )
        ],
        citations=[_citation()],
    )


def _conflict() -> s.CrossClauseConflict:
    return s.CrossClauseConflict(
        id="conflict-c12",
        sourceClauseId="3.2",
        targetClauseId="6.1",
        conflictType=s.ConflictType.payment_contradiction,
        description="付款违约口径冲突。",
        level=s.RiskLevel.medium,
        suggestion="统一违约口径。",
        citations=[_citation()],
    )


def _seal() -> s.SealSignatureDetection:
    return s.SealSignatureDetection(
        id="seal-c12",
        type=s.SealType.official_seal,
        status=s.SealStatus.valid,
        bbox=_bbox(),
        page=1,
        extractedText="北京示例科技有限公司",
        isMatchedWithParties=True,
        complianceNote="公章与主体一致。",
        confidence=0.98,
    )


def test_contract_query_routes_do_not_use_mock() -> None:
    path = Path(__file__).resolve().parents[1] / "contracts.py"
    text = path.read_text(encoding="utf-8")
    list_block = text.split("async def list_contracts", 1)[1].split(
        "@router.post",
        1,
    )[0]
    get_block = text.split("async def get_contract", 1)[1].split(
        "@router.get",
        1,
    )[0]

    assert "MockDataFactory" not in list_block
    assert "MockDataFactory" not in get_block


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 合同查询集成测试",
)
def test_contract_list_and_detail_are_aggregated() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_contract_query(POSTGRES_DSN))


async def _exercise_contract_query(dsn: str) -> None:
    schema = new_schema_name("cg_c12")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract_id = uuid4()
            created = await ContractRepository(session).create(
                ContractCreate(
                    id=contract_id,
                    filename="查询测试合同.docx",
                    file_size=2048,
                    file_hash="f" * 64,
                    storage_path="contracts/query-test.docx",
                    status=s.ContractStatus.pending_review,
                    contract_type=ContractType.PURCHASE,
                )
            )
            await ElementRepository(session).bulk_upsert(contract_id, [_chunk()])
            await RiskRepository(session).bulk_upsert(
                contract_id,
                [_risk(), _conflict()],
            )
            await SealRepository(session).bulk_upsert(contract_id, [_seal()])
            await ReportRepository(session).create(
                contract_id,
                "DOCX",
                "reports/query-test.docx",
            )
            await session.commit()

            context = service_context(session)
            app = FastAPI()
            app.include_router(contracts_router.router, prefix="/api/v1")
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                listed = await client.get(
                    "/api/v1/contracts",
                    params={
                        "page": 1,
                        "pageSize": 10,
                        "contractType": "PURCHASE",
                        "status": "PENDING_REVIEW",
                    },
                )
                assert listed.status_code == 200
                list_data = listed.json()["data"]
                assert list_data["total"] == 1
                assert list_data["items"][0]["riskCount"] == 1
                assert list_data["items"][0]["healthScore"]["overall"] == 77

                detail = await client.get(f"/api/v1/contracts/{contract_id}")
                assert detail.status_code == 200
                data = detail.json()["data"]
                assert data["fileKey"] == "contracts/query-test.docx"
                assert data["fileType"] == "DOCX"
                assert data["version"] == created.version
                assert data["elements"][0]["key"] == "3.2"
                assert data["risks"][0]["id"] == "risk-c12"
                assert data["conflicts"][0]["id"] == "conflict-c12"
                assert data["seals"][0]["id"] == "seal-c12"
                assert data["missingClauses"] == []
                assert data["reportId"] is not None

                missing = await client.get(f"/api/v1/contracts/{uuid4()}")
                assert missing.status_code == 404
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
