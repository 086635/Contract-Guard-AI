"""C22 解析结果入库与审核派发的测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

import pytest

from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.db.repository.result import ElementRepository, SealRepository
from app.generated import schemas as s
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.ingestion import IngestionService, clause_payload_to_chunk
from app.services.ports import TaskStatusSnapshot
from rag_contracts.chunk import ContractChunk
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


class FakeDispatcher:
    """可配置快照的 TaskDispatcherPort 替身。"""

    def __init__(self, snapshots: dict[str, TaskStatusSnapshot] | None = None) -> None:
        self.snapshots = snapshots or {}
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        self.calls.append((str(task_name), args))
        return task_id or f"fake-{len(self.calls)}"

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        return self.snapshots.get(task_id)


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


def _clause_payload() -> list[dict[str, Any]]:
    return [
        {
            "clause_id": "3.2",
            "label": "付款违约金",
            "text": "乙方逾期付款的，应按合同总额 30% 支付违约金。",
            "level": 1,
            "parent_id": None,
            "page_no": 1,
            "bbox": {
                "page": 1,
                "x": 72.0,
                "y": 720.0,
                "width": 300.0,
                "height": 40.0,
                "coordinateSystem": "PDF_POINTS",
            },
            "children_ids": [],
        },
        {
            "clause_id": "6.1",
            "label": "争议解决",
            "text": "双方发生争议协商不成的，提交北京仲裁委员会仲裁。",
            "level": 1,
            "parent_id": None,
            "page_no": 2,
            "bbox": None,
            "children_ids": [],
        },
    ]


def _seal_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": "seal-001-01",
            "type": "OFFICIAL_SEAL",
            "status": "VALID",
            "bbox": {
                "page": 1,
                "x": 300.0,
                "y": 400.0,
                "width": 100.0,
                "height": 100.0,
                "coordinateSystem": "PDF_POINTS",
            },
            "page": 1,
            "extractedText": "北京示例科技有限公司",
            "isMatchedWithParties": True,
            "complianceNote": "公章清晰且与合同主体一致。",
            "confidence": 0.97,
        }
    ]


def _done_parse_result(contract_id: UUID) -> dict[str, Any]:
    return {
        "contractId": str(contract_id),
        "traceId": "trace-1",
        "status": "SUCCESS",
        "fileType": "DOCX",
        "routeKind": "DOCX",
        "routeReason": "",
        "pageCount": 2,
        "lineCount": 10,
        "charCount": 100,
        "coordinatesSynthetic": True,
        "ocrUsed": False,
        "engineName": "",
        "elements": [],
        "clauses": _clause_payload(),
        "sealDetections": _seal_payload(),
        "layoutBlocks": [],
        "warnings": [],
        "stageLog": [],
        "artifactPath": None,
        "tookMs": 10,
    }


# ---------------------------------------------------------------------------
# 离线投影单测（不依赖 PostgreSQL）
# ---------------------------------------------------------------------------
def test_clause_payload_to_chunk_maps_fields() -> None:
    clause = _clause_payload()[0]
    chunk = clause_payload_to_chunk("contract-c22", clause, 1)

    assert chunk.chunk_id == ContractChunk.make_chunk_id("contract-c22", "3.2", 0)
    assert chunk.doc_id == "contract-c22"
    assert chunk.source_type.value == "STANDARD_CLAUSE"
    assert chunk.chunk_type.value == "CLAUSE"
    assert chunk.section_id == "3.2"
    assert chunk.title == "付款违约金"
    assert chunk.text == clause["text"]
    assert chunk.text_hash == ContractChunk.compute_text_hash(clause["text"])
    assert chunk.char_count == len(clause["text"])
    assert chunk.level == 1
    assert chunk.parent_section_id is None
    assert chunk.seq_in_doc == 1
    assert chunk.page_no == 1
    assert chunk.bbox is not None
    assert chunk.bbox.page == 1
    assert chunk.bbox.width == 300.0


def test_clause_payload_to_chunk_missing_bbox_yields_none() -> None:
    clause = _clause_payload()[1]
    chunk = clause_payload_to_chunk("contract-c22", clause, 2)
    assert chunk.bbox is None
    assert chunk.page_no == 2


def test_clause_payload_to_chunk_requires_id_and_text() -> None:
    with pytest.raises(ValueError, match="clause_id"):
        clause_payload_to_chunk("c", {"text": "正文"}, 1)
    with pytest.raises(ValueError, match="text"):
        clause_payload_to_chunk("c", {"clause_id": "3.2"}, 1)


def test_clause_payload_to_chunk_deterministic() -> None:
    clause = _clause_payload()[0]
    first = clause_payload_to_chunk("contract-c22", clause, 1)
    second = clause_payload_to_chunk("contract-c22", clause, 1)
    assert first.chunk_id == second.chunk_id
    assert first.text_hash == second.text_hash


# ---------------------------------------------------------------------------
# 真实 PostgreSQL 集成测试（未设 CG_POSTGRES_DSN 时 skip）
# ---------------------------------------------------------------------------
async def _exercise_ingestion_parse(dsn: str, schema: str) -> None:
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        async with factory() as session:
            contracts = ContractRepository(session)
            tasks = TaskRepository(session)
            elements = ElementRepository(session)
            seals = SealRepository(session)

            # ① DONE 快照：入库 + 派发
            contract = await contracts.create(
                ContractCreate(
                    filename="解析入库测试合同.docx",
                    file_size=4096,
                    file_hash="a" * 64,
                    storage_path="contracts/c22.docx",
                )
            )
            await session.commit()
            await tasks.create(contract.id, TaskKind.PARSE_DOC.name, "parse-done")
            await session.commit()

            fake = FakeDispatcher(
                {
                    "parse-done": TaskStatusSnapshot(
                        status="DONE",
                        stage="done",
                        percent=1.0,
                        result=_done_parse_result(contract.id),
                    )
                }
            )
            ctx: ServiceContext = service_context(
                session,
                task_dispatcher=fake,  # type: ignore[arg-type]
            )

            assert await IngestionService(ctx).finalize_parse(contract.id) is True

            chunks = await elements.list_by_contract(contract.id)
            assert [chunk.section_id for chunk in chunks] == ["3.2", "6.1"]
            assert chunks[0].title == "付款违约金"
            assert chunks[1].bbox is None

            loaded_seals = await seals.list_by_contract(contract.id)
            assert len(loaded_seals) == 1
            assert loaded_seals[0].id == "seal-001-01"
            assert loaded_seals[0].type == s.SealType.official_seal

            run_audit_calls = [
                c for c in fake.calls if c[0] == TaskKind.RUN_AUDIT.value
            ]
            assert len(run_audit_calls) == 1
            assert run_audit_calls[0][1][0] == str(contract.id)
            assert run_audit_calls[0][1][1] == _clause_payload()

            audit_task = await tasks.get_latest_by_contract(
                contract.id,
                TaskKind.RUN_AUDIT.name,
            )
            assert audit_task is not None
            assert audit_task.celery_task_id is not None

            # ② 幂等：重复 finalize 不重复派发、不重复建行
            assert await IngestionService(ctx).finalize_parse(contract.id) is True
            assert len([c for c in fake.calls if c[0] == TaskKind.RUN_AUDIT.value]) == 1
            assert len(await elements.list_by_contract(contract.id)) == 2

            # ③ RUNNING / FAILED 快照：不入库、不派发
            running_contract = await contracts.create(
                ContractCreate(
                    filename="运行中合同.docx",
                    file_size=1024,
                    file_hash="b" * 64,
                    storage_path="contracts/running.docx",
                )
            )
            await session.commit()
            await tasks.create(
                running_contract.id,
                TaskKind.PARSE_DOC.name,
                "parse-running",
            )
            await session.commit()

            fake.snapshots["parse-running"] = TaskStatusSnapshot(
                status="RUNNING",
                stage="parse_text",
                percent=0.5,
                result=None,
            )
            assert (
                await IngestionService(ctx).finalize_parse(running_contract.id) is False
            )
            assert await elements.list_by_contract(running_contract.id) == []

            failed_contract = await contracts.create(
                ContractCreate(
                    filename="失败合同.docx",
                    file_size=1024,
                    file_hash="c" * 64,
                    storage_path="contracts/failed.docx",
                )
            )
            await session.commit()
            await tasks.create(
                failed_contract.id,
                TaskKind.PARSE_DOC.name,
                "parse-failed",
            )
            await session.commit()

            fake.snapshots["parse-failed"] = TaskStatusSnapshot(
                status="FAILED",
                stage="failed",
                percent=0.3,
                result={"reason": "ocr failed"},
            )
            assert (
                await IngestionService(ctx).finalize_parse(failed_contract.id) is False
            )
            assert await elements.list_by_contract(failed_contract.id) == []
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=("未设置 CG_POSTGRES_DSN，跳过 C22 解析入库集成测试（保持离线基线）"),
)
def test_ingestion_parse_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    schema = new_schema_name("cg_c22")

    _run(create_schema(POSTGRES_DSN, schema))
    try:
        _run(migrate(POSTGRES_DSN, schema))
        _run(_exercise_ingestion_parse(POSTGRES_DSN, schema))
    finally:
        _run(drop_schema(POSTGRES_DSN, schema))


def test_ingestion_has_no_mock_factory() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "ingestion.py"
    ).read_text(encoding="utf-8")
    assert "MockDataFactory" not in source
