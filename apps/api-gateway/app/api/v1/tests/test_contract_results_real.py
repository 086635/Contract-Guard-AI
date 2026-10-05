"""C13 合同结果查询真实化测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.service_testkit import (
    create_schema,
    drop_schema,
    install_test_auth,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)

from app.api.deps import get_service_context
from app.api.v1 import contracts as contracts_router
from app.db.models import AuditTask
from app.db.repository.contract import ContractCreate, ContractRepository
from app.db.repository.result import (
    ElementRepository,
    RiskRepository,
    SealRepository,
)
from app.generated import schemas as s
from app.services.ingestion import element_payload_to_chunk
from app.services.projector import ContractProjector
from rag_contracts.chunk import BBox as ChunkBBox
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


def _api_bbox() -> s.BBox:
    return s.BBox(
        page=1,
        x=72.0,
        y=720.0,
        width=300.0,
        height=40.0,
        coordinateSystem=s.CoordinateSystem.pdf_points,
    )


def _chunk(section_id: str, text: str) -> ContractChunk:
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id("contract-c13", section_id),
        doc_id="contract-c13",
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id=section_id,
        section_path=f"第三章/{section_id}",
        seq_in_doc=int(section_id.split(".")[-1]),
        title=f"条款 {section_id}",
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=6,
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


def _element_chunk(
    key: str,
    value: str | None,
    label: str,
    confidence: float,
    is_custom: bool,
) -> ContractChunk:
    bbox = None
    if value is not None:
        bbox = {
            "page": 1,
            "x": 72.0,
            "y": 720.0,
            "width": 300.0,
            "height": 40.0,
        }
    return element_payload_to_chunk(
        "contract-c13",
        {
            "key": key,
            "label": label,
            "value": value,
            "confidence": confidence,
            "bbox": bbox,
            "isCustom": is_custom,
        },
    )


def test_element_extra_projection_roundtrip() -> None:
    chunk = _element_chunk(
        key="partyA",
        value="北京示例科技有限公司",
        label="甲方",
        confidence=0.93,
        is_custom=False,
    )
    assert chunk.category == "CONTRACT_ELEMENT"
    assert chunk.extra == {
        "value": "北京示例科技有限公司",
        "confidence": 0.93,
        "isCustom": False,
    }
    projected = ContractProjector.elements([chunk])[0]
    assert projected.key == "partyA"
    assert projected.label == "甲方"
    assert projected.value == "北京示例科技有限公司"
    assert projected.confidence == 0.93
    assert projected.is_custom is False

    null_chunk = _element_chunk(
        key="guaranteePeriod",
        value=None,
        label="质保期",
        confidence=0.4,
        is_custom=True,
    )
    null_projected = ContractProjector.elements([null_chunk])[0]
    assert null_projected.value is None
    assert null_projected.label == "质保期"
    assert null_projected.is_custom is True

def _risk(risk_id: str, level: s.RiskLevel) -> s.EnhancedRiskItem:
    return s.EnhancedRiskItem(
        id=risk_id,
        clauseId="3.2",
        category=s.RiskCategory.penalty_too_high,
        level=level,
        title="风险",
        originalText="原始文本",
        bbox=_api_bbox(),
        suggestion="处理建议",
        legalBasis=None,
        reasoningSteps=[],
        citations=[],
    )


def _conflict() -> s.CrossClauseConflict:
    return s.CrossClauseConflict(
        id="conflict-c13",
        sourceClauseId="3.2",
        targetClauseId="6.1",
        conflictType=s.ConflictType.payment_contradiction,
        description="矛盾",
        level=s.RiskLevel.medium,
        suggestion="统一",
        citations=[],
    )


def _seal() -> s.SealSignatureDetection:
    return s.SealSignatureDetection(
        id="seal-c13",
        type=s.SealType.official_seal,
        status=s.SealStatus.valid,
        bbox=_api_bbox(),
        page=1,
        extractedText="示例公司",
        isMatchedWithParties=True,
        complianceNote="合规",
        confidence=0.99,
    )


def test_element_projection_rejects_invalid_payload() -> None:
    with pytest.raises(ValueError, match="key"):
        element_payload_to_chunk(
            "contract-c13",
            {"key": "", "label": "甲方", "value": "x", "confidence": 0.9,
             "bbox": None, "isCustom": False},
        )
    with pytest.raises(ValueError, match="confidence"):
        element_payload_to_chunk(
            "contract-c13",
            {"key": "partyA", "label": "甲方", "value": "x", "confidence": 1.2,
             "bbox": None, "isCustom": False},
        )

def test_result_route_blocks_have_no_mock() -> None:
    path = Path(__file__).resolve().parents[1] / "contracts.py"
    text = path.read_text(encoding="utf-8")
    for marker, end_marker in (
        ("async def get_parse_status", "@router.get"),
        ("async def get_elements", "@router.get"),
        ("async def get_risks", "@router.get"),
        ("async def get_conflicts", "@router.get"),
        ("async def get_seals", "@router.post"),
    ):
        block = text.split(marker, 1)[1].split(end_marker, 1)[0]
        assert "MockDataFactory" not in block


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 合同结果查询集成测试",
)
def test_contract_results_read_from_repositories() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_contract_results(POSTGRES_DSN))


async def _exercise_contract_results(dsn: str) -> None:
    schema = new_schema_name("cg_c13")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract_id = uuid4()
            await ContractRepository(session).create(
                ContractCreate(
                    id=contract_id,
                    filename="结果测试合同.docx",
                    file_size=2048,
                    file_hash="d" * 64,
                    storage_path="contracts/result-c13.docx",
                    status=s.ContractStatus.pending_review,
                    contract_type=ContractType.PURCHASE,
                )
            )
            await ElementRepository(session).bulk_upsert(
                contract_id,
                [
                    _chunk("3.1", "第一条正文。"),
                    _chunk("3.2", "第二条正文。"),
                    _element_chunk(
                        key="partyA",
                        value="北京示例科技有限公司",
                        label="甲方",
                        confidence=0.93,
                        is_custom=False,
                    ),
                    _element_chunk(
                        key="guaranteePeriod",
                        value=None,
                        label="质保期",
                        confidence=0.40,
                        is_custom=True,
                    ),
                ],
            )
            await RiskRepository(session).bulk_upsert(
                contract_id,
                [
                    _risk("risk-high", s.RiskLevel.high),
                    _risk("risk-low", s.RiskLevel.low),
                    _conflict(),
                ],
            )
            await SealRepository(session).bulk_upsert(contract_id, [_seal()])

            first = AuditTask(
                contract_id=contract_id,
                task_type="PARSE_DOC",
                stage="parse_text",
                progress=0.5,
                status=s.Status.running,
                celery_task_id="celery-old",
                created_at=datetime.now(UTC) - timedelta(minutes=1),
                updated_at=datetime.now(UTC) - timedelta(minutes=1),
            )
            latest = AuditTask(
                contract_id=contract_id,
                task_type="PARSE_DOC",
                stage="done",
                progress=1.0,
                status=s.Status.done,
                celery_task_id="celery-new",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add_all([first, latest])
            await session.commit()

            context = service_context(session)
            app = FastAPI()
            app.include_router(contracts_router.router, prefix="/api/v1")
            install_test_auth(app, session)
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                parse_status = await client.get(
                    f"/api/v1/contracts/{contract_id}/parse-status"
                )
                assert parse_status.status_code == 200
                assert parse_status.json()["data"]["taskId"] == str(latest.id)
                assert parse_status.json()["data"]["status"] == "DONE"

                elements = await client.get(f"/api/v1/contracts/{contract_id}/elements")
                assert elements.status_code == 200
                element_items = {
                    item["key"]: item for item in elements.json()["data"]
                }
                assert set(element_items) == {
                    "3.1",
                    "3.2",
                    "partyA",
                    "guaranteePeriod",
                }
                assert element_items["partyA"] == {
                    "key": "partyA",
                    "label": "甲方",
                    "value": "北京示例科技有限公司",
                    "confidence": 0.93,
                    "bbox": element_items["partyA"]["bbox"],
                    "isCustom": False,
                }
                assert element_items["guaranteePeriod"]["value"] is None
                assert element_items["guaranteePeriod"]["label"] == "质保期"
                assert element_items["guaranteePeriod"]["isCustom"] is True

                risks = await client.get(
                    f"/api/v1/contracts/{contract_id}/risks",
                    params={"level": "HIGH"},
                )
                assert risks.status_code == 200
                assert [item["id"] for item in risks.json()["data"]] == ["risk-high"]

                conflicts = await client.get(
                    f"/api/v1/contracts/{contract_id}/conflicts"
                )
                assert conflicts.status_code == 200
                assert conflicts.json()["data"][0]["id"] == "conflict-c13"

                seals = await client.get(f"/api/v1/contracts/{contract_id}/seals")
                assert seals.status_code == 200
                assert seals.json()["data"][0]["id"] == "seal-c13"

                invalid_level = await client.get(
                    f"/api/v1/contracts/{contract_id}/risks",
                    params={"level": "INFO"},
                )
                assert invalid_level.status_code == 422

                missing = await client.get(f"/api/v1/contracts/{uuid4()}/elements")
                assert missing.status_code == 404
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
