"""C16/C21 报告生成、结果落库与下载测试。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
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
from app.api.v1 import report as report_router
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.db.repository.result import ReportRepository
from app.generated import schemas as s
from app.services.ports import StoredObjectInfo, TaskStatusSnapshot
from app.services.report_service import (
    ReportNotFoundError,
    ReportService,
    ReportUnavailableError,
)
from app.services.result_service import ResultService
from rag_contracts.enums import ContractType

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")


class FakeStorage:
    """报告预签名测试替身。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        return f"https://minio.test/{file_key}?method=PUT"

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        if self.fail:
            raise RuntimeError("minio unavailable")
        self.calls.append((file_key, expires_s))
        return f"https://minio.test/{file_key}?method=GET"

    def stat_object(self, file_key: str) -> StoredObjectInfo:
        return StoredObjectInfo(size=1024, etag="etag")


class FakeDispatcher:
    """报告任务派发与状态查询测试替身。"""

    def __init__(self, *, status: TaskStatusSnapshot | None = None) -> None:
        self.calls: list[tuple[str, tuple[object, ...], str | None]] = []
        self.status = status

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        self.calls.append((task_name, args, task_id))
        return "celery-report-1"

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        del task_id
        return self.status


class _FakeDto:
    """只实现 C21 需要的 `model_dump` 协议。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return dict(self._payload)


def _report_record(
    contract_id: UUID,
    storage_path: str = "reports/c21.docx",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        contract_id=contract_id,
        format="DOCX",
        storage_path=storage_path,
        download_count=0,
        generated_at=datetime.now(UTC),
        status=s.Status.done,
    )


def _service(
    session: AsyncSession,
    *,
    dispatcher: FakeDispatcher,
    storage: FakeStorage,
) -> ReportService:
    return ReportService(
        service_context(
            session,
            object_storage=storage,
            task_dispatcher=dispatcher,
        )
    )


def test_report_router_has_no_mock_import() -> None:
    path = Path(__file__).resolve().parents[1] / "report.py"
    text = path.read_text(encoding="utf-8")

    assert "MockDataFactory" not in text


def test_report_service_has_no_mock_import() -> None:
    path = (
        Path(__file__).resolve().parents[4] / "app" / "services" / "report_service.py"
    )
    text = path.read_text(encoding="utf-8")

    assert "MockDataFactory" not in text


def test_generate_builds_payload_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_exercise_generate_payload(monkeypatch))


async def _exercise_generate_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_id = uuid4()
    session = cast(AsyncSession, AsyncMock())
    dispatcher = FakeDispatcher()
    storage = FakeStorage()
    monkeypatch.setattr(
        ContractRepository,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=contract_id)),
    )
    monkeypatch.setattr(
        ResultService,
        "risks",
        AsyncMock(return_value=[_FakeDto({"id": "risk-1"})]),
    )
    monkeypatch.setattr(
        ResultService,
        "conflicts",
        AsyncMock(return_value=[_FakeDto({"id": "conflict-1"})]),
    )
    monkeypatch.setattr(
        ResultService,
        "elements",
        AsyncMock(return_value=[_FakeDto({"key": "partyA"})]),
    )
    monkeypatch.setattr(
        TaskRepository,
        "create",
        AsyncMock(
            return_value=SimpleNamespace(
                id=uuid4(),
                stage="queued",
                progress=0.0,
                status=s.Status.running,
            )
        ),
    )

    result = await _service(
        session,
        dispatcher=dispatcher,
        storage=storage,
    ).generate(contract_id)

    assert result.status == s.Status.running
    assert dispatcher.calls[0][0] == "gen_report"
    args = dispatcher.calls[0][1]
    assert args[0] == str(contract_id)
    payload = cast(dict[str, Any], args[1])
    assert payload["contractId"] == str(contract_id)
    assert payload["risks"] == [{"id": "risk-1"}]
    assert payload["conflicts"] == [{"id": "conflict-1"}]
    assert payload["elements"] == [{"key": "partyA"}]
    assert "generatedAt" in payload
    cast(AsyncMock, session.commit).assert_awaited()


def test_get_finalizes_done_snapshot_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_done_finalize(monkeypatch))


async def _exercise_done_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_id = uuid4()
    session = cast(AsyncSession, AsyncMock())
    dispatcher = FakeDispatcher(
        status=TaskStatusSnapshot(
            status="DONE",
            stage="done",
            percent=1.0,
            result={"storagePath": "reports/c21.docx"},
        )
    )
    storage = FakeStorage()
    report = _report_record(contract_id)
    monkeypatch.setattr(
        ContractRepository,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=contract_id)),
    )
    monkeypatch.setattr(
        ReportRepository,
        "get",
        AsyncMock(side_effect=[None, report]),
    )
    monkeypatch.setattr(
        TaskRepository,
        "get_latest_by_contract",
        AsyncMock(return_value=SimpleNamespace(id=uuid4(), celery_task_id="celery-1")),
    )
    report_create = AsyncMock(return_value=report)
    monkeypatch.setattr(ReportRepository, "create", report_create)
    service = _service(session, dispatcher=dispatcher, storage=storage)

    first = await service.get(contract_id)
    second = await service.get(contract_id)

    assert first.download_url.endswith("method=GET")
    assert second.download_url.endswith("method=GET")
    assert report_create.await_count == 1
    assert report_create.await_args.args == (
        contract_id,
        "DOCX",
        "reports/c21.docx",
    )
    assert cast(AsyncMock, session.commit).await_count == 1
    assert storage.calls == [
        ("reports/c21.docx", 900),
        ("reports/c21.docx", 900),
    ]


def test_get_running_snapshot_returns_not_found_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_running_snapshot(monkeypatch))


async def _exercise_running_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_id = uuid4()
    session = cast(AsyncSession, AsyncMock())
    dispatcher = FakeDispatcher(
        status=TaskStatusSnapshot(
            status="RUNNING",
            stage="render",
            percent=0.6,
            result=None,
        )
    )
    monkeypatch.setattr(
        ContractRepository,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=contract_id)),
    )
    monkeypatch.setattr(ReportRepository, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        TaskRepository,
        "get_latest_by_contract",
        AsyncMock(return_value=SimpleNamespace(id=uuid4(), celery_task_id="celery-1")),
    )
    report_create = AsyncMock()
    monkeypatch.setattr(ReportRepository, "create", report_create)

    with pytest.raises(ReportNotFoundError):
        await _service(
            session,
            dispatcher=dispatcher,
            storage=FakeStorage(),
        ).get(contract_id)

    report_create.assert_not_awaited()


def test_get_failed_snapshot_returns_unavailable_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_failed_snapshot(monkeypatch))


async def _exercise_failed_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_id = uuid4()
    session = cast(AsyncSession, AsyncMock())
    dispatcher = FakeDispatcher(
        status=TaskStatusSnapshot(
            status="FAILED",
            stage="upload",
            percent=0.95,
            result={"warnings": ["上传失败"]},
        )
    )
    monkeypatch.setattr(
        ContractRepository,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=contract_id)),
    )
    monkeypatch.setattr(ReportRepository, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        TaskRepository,
        "get_latest_by_contract",
        AsyncMock(return_value=SimpleNamespace(id=uuid4(), celery_task_id="celery-1")),
    )

    with pytest.raises(ReportUnavailableError, match="上传失败"):
        await _service(
            session,
            dispatcher=dispatcher,
            storage=FakeStorage(),
        ).get(contract_id)


def test_get_done_without_storage_path_returns_unavailable_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_missing_storage_path(monkeypatch))


async def _exercise_missing_storage_path(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_id = uuid4()
    session = cast(AsyncSession, AsyncMock())
    dispatcher = FakeDispatcher(
        status=TaskStatusSnapshot(
            status="DONE",
            stage="done",
            percent=1.0,
            result={},
        )
    )
    monkeypatch.setattr(
        ContractRepository,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=contract_id)),
    )
    monkeypatch.setattr(ReportRepository, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(
        TaskRepository,
        "get_latest_by_contract",
        AsyncMock(return_value=SimpleNamespace(id=uuid4(), celery_task_id="celery-1")),
    )
    report_create = AsyncMock()
    monkeypatch.setattr(ReportRepository, "create", report_create)

    with pytest.raises(ReportUnavailableError, match="storagePath"):
        await _service(
            session,
            dispatcher=dispatcher,
            storage=FakeStorage(),
        ).get(contract_id)

    report_create.assert_not_awaited()


def test_get_existing_report_presign_failure_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asyncio.run(_exercise_presign_failure(monkeypatch))


async def _exercise_presign_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    contract_id = uuid4()
    session = cast(AsyncSession, AsyncMock())
    report = _report_record(contract_id)
    monkeypatch.setattr(
        ContractRepository,
        "get",
        AsyncMock(return_value=SimpleNamespace(id=contract_id)),
    )
    monkeypatch.setattr(ReportRepository, "get", AsyncMock(return_value=report))

    with pytest.raises(ReportUnavailableError, match="minio unavailable"):
        await _service(
            session,
            dispatcher=FakeDispatcher(),
            storage=FakeStorage(fail=True),
        ).get(contract_id)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 PostgreSQL 报告任务集成测试",
)
def test_report_generate_and_download() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_reports(POSTGRES_DSN))


async def _exercise_reports(dsn: str) -> None:
    schema = new_schema_name("cg_c16")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contracts = ContractRepository(session)
            contract_id = uuid4()
            await contracts.create(
                ContractCreate(
                    id=contract_id,
                    filename="报告测试合同.docx",
                    file_size=2048,
                    file_hash="b" * 64,
                    storage_path="contracts/report-c16.docx",
                    status=s.ContractStatus.finalized,
                    contract_type=ContractType.PURCHASE,
                )
            )
            await ReportRepository(session).create(
                contract_id,
                "DOCX",
                "reports/c16.docx",
            )
            missing_report_id = uuid4()
            await contracts.create(
                ContractCreate(
                    id=missing_report_id,
                    filename="无报告合同.docx",
                    file_size=1024,
                    file_hash="c" * 64,
                    storage_path="contracts/no-report.docx",
                    status=s.ContractStatus.finalized,
                    contract_type=ContractType.PURCHASE,
                )
            )
            await session.commit()

            storage = FakeStorage()
            dispatcher = FakeDispatcher()
            context = service_context(
                session,
                object_storage=storage,
                task_dispatcher=dispatcher,
            )
            app = FastAPI()
            app.include_router(report_router.router, prefix="/api/v1")
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                generated = await client.post(
                    f"/api/v1/report/{contract_id}/generate",
                    headers={"X-Request-ID": str(uuid4())},
                )
                assert generated.status_code == 202
                assert generated.json()["data"]["status"] == "RUNNING"
                assert dispatcher.calls[0][0] == "gen_report"
                assert dispatcher.calls[0][1][0] == str(contract_id)
                payload = cast(dict[str, Any], dispatcher.calls[0][1][1])
                assert payload["contractId"] == str(contract_id)
                assert {"risks", "conflicts", "elements"} <= set(payload)

                report = await client.get(f"/api/v1/report/{contract_id}")
                assert report.status_code == 200
                assert report.json()["data"]["downloadUrl"].endswith("method=GET")
                assert storage.calls == [("reports/c16.docx", 900)]

                no_report = await client.get(f"/api/v1/report/{missing_report_id}")
                assert no_report.status_code == 404

                missing = await client.post(
                    f"/api/v1/report/{uuid4()}/generate",
                    headers={"X-Request-ID": str(uuid4())},
                )
                assert missing.status_code == 404

                storage.fail = True
                unavailable = await client.get(f"/api/v1/report/{contract_id}")
                assert unavailable.status_code == 502
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
