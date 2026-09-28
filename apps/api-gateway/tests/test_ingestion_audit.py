"""C23 审核结果入库的测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.db.repository.result import RiskRepository
from app.generated import schemas as s
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.ingestion import IngestionService
from app.services.ports import TaskStatusSnapshot
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
_INGESTION = Path(__file__).resolve().parents[1] / "app" / "services" / "ingestion.py"


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


def _risk() -> s.EnhancedRiskItem:
    return s.EnhancedRiskItem(
        id="risk-c23-1",
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
        id="conflict-c23-1",
        sourceClauseId="3.2",
        targetClauseId="6.1",
        conflictType=s.ConflictType.payment_contradiction,
        description="付款违约金与逾期利息口径冲突。",
        level=s.RiskLevel.medium,
        suggestion="统一付款违约责任口径。",
        citations=[_citation()],
    )


def _audit_result(contract_id: UUID) -> dict[str, Any]:
    """对齐 `worker.tasks.run_audit._payload` 顶层结构。"""
    return {
        "contractId": str(contract_id),
        "traceId": "trace-audit-1",
        "status": "SUCCESS",
        "risks": [_risk().model_dump(mode="json", by_alias=True)],
        "conflicts": [_conflict().model_dump(mode="json", by_alias=True)],
        "missingClauses": [],
        "healthScore": {
            "overall": 80,
            "dimensions": {
                "compliance": 80.0,
                "performanceRisk": 20.0,
                "clauseCompleteness": 100.0,
            },
        },
        "warnings": [],
        "stageLog": [],
        "tookMs": 10,
    }


# ---------------------------------------------------------------------------
# 离线守卫（不依赖 PostgreSQL）
# ---------------------------------------------------------------------------
def test_ingestion_service_is_single_definition_with_finalize_audit() -> None:
    source = _INGESTION.read_text(encoding="utf-8")
    assert source.count("class IngestionService") == 1
    assert "async def finalize_audit" in source


def test_ingestion_audit_has_no_mock_factory() -> None:
    assert "MockDataFactory" not in _INGESTION.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 真实 PostgreSQL 集成测试（未设 CG_POSTGRES_DSN 时 skip）
# ---------------------------------------------------------------------------
async def _exercise_ingestion_audit(dsn: str, schema: str) -> None:
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        async with factory() as session:
            contracts = ContractRepository(session)
            tasks = TaskRepository(session)
            risks = RiskRepository(session)

            # ① DONE：入库 risks + conflicts
            contract = await contracts.create(
                ContractCreate(
                    filename="审核入库测试合同.docx",
                    file_size=4096,
                    file_hash="a" * 64,
                    storage_path="contracts/c23.docx",
                )
            )
            await session.commit()
            await tasks.create(contract.id, TaskKind.RUN_AUDIT.name, "audit-done")
            await session.commit()

            fake = FakeDispatcher(
                {
                    "audit-done": TaskStatusSnapshot(
                        status="DONE",
                        stage="done",
                        percent=1.0,
                        result=_audit_result(contract.id),
                    )
                }
            )
            ctx: ServiceContext = service_context(
                session,
                task_dispatcher=fake,  # type: ignore[arg-type]
            )

            assert await IngestionService(ctx).finalize_audit(contract.id) is True

            loaded_risks = await risks.list_by_contract(contract.id)
            loaded_conflicts = await risks.list_conflicts_by_contract(contract.id)
            assert [risk.id for risk in loaded_risks] == ["risk-c23-1"]
            assert loaded_risks[0].level == s.RiskLevel.high
            assert loaded_risks[0].category == s.RiskCategory.penalty_too_high
            assert (
                loaded_risks[0].citations[0].source_title == "《民法典》第五百八十五条"
            )
            assert [item.id for item in loaded_conflicts] == ["conflict-c23-1"]
            assert loaded_conflicts[0].level == s.RiskLevel.medium

            # ② 幂等：重复 finalize 不重复建行
            assert await IngestionService(ctx).finalize_audit(contract.id) is True
            assert len(await risks.list_by_contract(contract.id)) == 1
            assert len(await risks.list_conflicts_by_contract(contract.id)) == 1

            # ③ RUNNING / FAILED / 无任务：不入库
            running = await contracts.create(
                ContractCreate(
                    filename="审核运行中.docx",
                    file_size=1024,
                    file_hash="b" * 64,
                    storage_path="contracts/audit-running.docx",
                )
            )
            await session.commit()
            await tasks.create(running.id, TaskKind.RUN_AUDIT.name, "audit-running")
            await session.commit()
            fake.snapshots["audit-running"] = TaskStatusSnapshot(
                status="RUNNING",
                stage="risk",
                percent=0.5,
                result=None,
            )
            assert await IngestionService(ctx).finalize_audit(running.id) is False
            assert await risks.list_by_contract(running.id) == []

            failed = await contracts.create(
                ContractCreate(
                    filename="审核失败.docx",
                    file_size=1024,
                    file_hash="c" * 64,
                    storage_path="contracts/audit-failed.docx",
                )
            )
            await session.commit()
            await tasks.create(failed.id, TaskKind.RUN_AUDIT.name, "audit-failed")
            await session.commit()
            fake.snapshots["audit-failed"] = TaskStatusSnapshot(
                status="FAILED",
                stage="failed",
                percent=0.3,
                result={"reason": "graph failed"},
            )
            assert await IngestionService(ctx).finalize_audit(failed.id) is False
            assert await risks.list_by_contract(failed.id) == []

            no_task = await contracts.create(
                ContractCreate(
                    filename="无审核任务.docx",
                    file_size=1024,
                    file_hash="d" * 64,
                    storage_path="contracts/audit-none.docx",
                )
            )
            await session.commit()
            assert await IngestionService(ctx).finalize_audit(no_task.id) is False
    finally:
        await engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason=("未设置 CG_POSTGRES_DSN，跳过 C23 审核结果入库集成测试（保持离线基线）"),
)
def test_ingestion_audit_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    schema = new_schema_name("cg_c23")

    _run(create_schema(POSTGRES_DSN, schema))
    try:
        _run(migrate(POSTGRES_DSN, schema))
        _run(_exercise_ingestion_audit(POSTGRES_DSN, schema))
    finally:
        _run(drop_schema(POSTGRES_DSN, schema))
