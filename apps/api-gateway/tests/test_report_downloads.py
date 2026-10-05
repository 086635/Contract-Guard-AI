"""C72 报告双格式下载字段测试（离线替身，无需 PostgreSQL/MinIO）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository.contract import ContractDetail
from app.db.repository.result import ReportRecord
from app.generated.schemas import Status
from app.services.context import ServiceContext
from app.services.ports import (
    ObjectStoragePort,
    TaskDispatcherPort,
    TaskStatusSnapshot,
)
from app.services.report_service import ReportService
from tests.service_testkit import service_context

CONTRACT_ID = UUID("00000000-0000-0000-0000-0000000000c2")
DOCX_KEY = "reports/c2/20261002120000_aaaaaaaa.docx"
PDF_KEY = "reports/c2/20261002120000_bbbbbbbb.pdf"


class FakeObjectStorage:
    """记录 presign 调用；URL 里带 key，便于断言两格式不混用。"""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, int]] = []

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        raise AssertionError("报告下载不签发 PUT URL")

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        self.get_calls.append((file_key, expires_s))
        return f"https://minio.test/{file_key}?sig=stub"

    def stat_object(self, file_key: str) -> Any:
        raise AssertionError("报告下载不读 stat")

    def bucket_exists(self) -> bool:
        return True

    def download_object(self, file_key: str, target_path: str) -> str:
        raise AssertionError("报告下载不下载对象")


class FakeSession:
    """只实现 commit 的 session 替身（finalize 路径会提交）。"""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeContractRepository:
    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists

    async def get(self, contract_id: UUID) -> ContractDetail | None:
        if not self._exists:
            return None
        return cast(ContractDetail, object())


class FakeTaskRepository:
    def __init__(self, task: Any | None) -> None:
        self._task = task

    async def get_latest_by_contract(
        self, contract_id: UUID, task_name: str
    ) -> Any | None:
        del contract_id, task_name
        return self._task


class FakeDispatcher:
    def __init__(self, snapshot: TaskStatusSnapshot | None = None) -> None:
        self.snapshot = snapshot

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        raise AssertionError("下载路径不派发任务")

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        del task_id
        return self.snapshot


class FakeReportRepository:
    """内存报告仓储：记录 create 入参并回放 get。"""

    def __init__(self, record: ReportRecord | None = None) -> None:
        self.record = record
        self.created: list[dict[str, Any]] = []

    async def get(self, contract_id: UUID) -> ReportRecord | None:
        del contract_id
        return self.record

    async def create(
        self,
        contract_id: UUID,
        format: str,
        storage_path: str,
        *,
        pdf_storage_path: str | None = None,
    ) -> ReportRecord:
        self.created.append(
            {
                "contract_id": contract_id,
                "format": format,
                "storage_path": storage_path,
                "pdf_storage_path": pdf_storage_path,
            }
        )
        self.record = _record(
            storage_path=storage_path, pdf_storage_path=pdf_storage_path
        )
        return self.record


def _record(
    *, storage_path: str = DOCX_KEY, pdf_storage_path: str | None = PDF_KEY
) -> ReportRecord:
    return ReportRecord(
        id=uuid4(),
        contract_id=CONTRACT_ID,
        format="DOCX",
        storage_path=storage_path,
        pdf_storage_path=pdf_storage_path,
        download_count=0,
        generated_at=datetime.now(UTC),
        status=Status.done,
    )


def _service(
    *,
    report: ReportRecord | None = None,
    task: Any | None = None,
    snapshot: TaskStatusSnapshot | None = None,
    contracts_exist: bool = True,
) -> tuple[ReportService, FakeReportRepository, FakeObjectStorage]:
    storage = FakeObjectStorage()
    context: ServiceContext = service_context(
        cast(AsyncSession, FakeSession()),
        object_storage=cast(ObjectStoragePort, storage),
        task_dispatcher=cast(TaskDispatcherPort, FakeDispatcher(snapshot)),
    )
    reports = FakeReportRepository(report)
    service = ReportService(
        context,
        contracts=cast(Any, FakeContractRepository(exists=contracts_exist)),
        tasks=cast(Any, FakeTaskRepository(task)),
        reports=cast(Any, reports),
    )
    return service, reports, storage


def test_docx_pdf_urls_distinct() -> None:
    """DOCX 与 PDF 必须是两个不同的对象/URL，不得互相复用（C72 §⑨-1）。"""
    service, _, storage = _service(report=_record())

    data = asyncio.run(service.get(CONTRACT_ID))

    assert data.word_download_url is not None
    assert data.pdf_download_url is not None
    assert data.word_download_url != data.pdf_download_url
    assert data.word_download_url.endswith(".docx?sig=stub")
    assert data.pdf_download_url.endswith(".pdf?sig=stub")
    assert [key for key, _ in storage.get_calls] == [DOCX_KEY, PDF_KEY]
    assert {expires for _, expires in storage.get_calls} == {900}


def test_regeneration_returns_latest_file_and_preserves_history() -> None:
    old = _record()
    new_key = "reports/c2/regenerated.docx"
    service, reports, _ = _service(report=old, task=SimpleNamespace(id=uuid4(), celery_task_id="new-report"), snapshot=TaskStatusSnapshot("DONE", "finished", 1, {"storagePath": new_key}))
    data = asyncio.run(service.get(CONTRACT_ID))
    assert new_key in data.word_download_url
    assert reports.created[0]["storage_path"] == new_key
    assert old.storage_path == DOCX_KEY
    asyncio.run(service.get(CONTRACT_ID))
    assert len(reports.created) == 1


def test_legacy_download_url_preserved() -> None:
    """旧 downloadUrl 继续可用且指向 DOCX（C72 §⑥.3）。"""
    service, _, storage = _service(report=_record())

    data = asyncio.run(service.get(CONTRACT_ID))

    assert data.download_url == f"https://minio.test/{DOCX_KEY}?sig=stub"
    assert data.download_url == data.word_download_url
    assert storage.get_calls[0][0] == DOCX_KEY


def test_pdf_url_absent_when_partial() -> None:
    """PDF 未生成时 pdfDownloadUrl=None，且不签发假 URL。"""
    service, _, storage = _service(
        report=_record(pdf_storage_path=None),
    )

    data = asyncio.run(service.get(CONTRACT_ID))

    assert data.pdf_download_url is None
    assert data.download_url is not None
    assert data.word_download_url == data.download_url
    assert [key for key, _ in storage.get_calls] == [DOCX_KEY]


def test_finalize_reads_pdf_storage_path_from_task_result() -> None:
    """网关按 worker 结果落库两个对象 key（DOCX + PDF）。"""
    snapshot = TaskStatusSnapshot(
        status="DONE",
        stage="done",
        percent=1.0,
        result={
            "status": "DONE",
            "storagePath": DOCX_KEY,
            "pdfStoragePath": PDF_KEY,
        },
    )
    service, reports, _ = _service(
        report=None,
        task=SimpleNamespace(celery_task_id="celery-report-1"),
        snapshot=snapshot,
    )

    data = asyncio.run(service.get(CONTRACT_ID))

    assert reports.created == [
        {
            "contract_id": CONTRACT_ID,
            "format": "DOCX",
            "storage_path": DOCX_KEY,
            "pdf_storage_path": PDF_KEY,
        }
    ]
    assert data.pdf_download_url is not None


def test_finalize_tolerates_degraded_result() -> None:
    """部分失败（worker status=DEGRADED）仍落库 DOCX，PDF 字段留空。"""
    snapshot = TaskStatusSnapshot(
        status="DONE",
        stage="done",
        percent=1.0,
        result={
            "status": "DEGRADED",
            "storagePath": DOCX_KEY,
            "pdfStoragePath": "",
            "warnings": ["PDF_EXPORT_FAILED:RuntimeError"],
        },
    )
    service, reports, _ = _service(
        report=None,
        task=SimpleNamespace(celery_task_id="celery-report-2"),
        snapshot=snapshot,
    )

    data = asyncio.run(service.get(CONTRACT_ID))

    assert reports.created[0]["pdf_storage_path"] is None
    assert data.pdf_download_url is None
    assert data.download_url is not None
