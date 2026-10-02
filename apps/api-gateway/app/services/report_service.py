"""审核报告生成与下载 Service。

设计要点：
1. 网关只派发 `gen_report`，并从 DAL 组装 worker 载荷，不生成 DOCX。
2. `audit_reports` 由网关按 Celery 成功结果落库，worker 不直连 PostgreSQL。
3. downloadUrl 每次查询运行时签发，不写入数据库。

对应：`文件集合/codex/4.C16报告生成与下载.md`、
`文件集合/codex/4.C21报告元数据落库.md`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.db.repository.contract import ContractRepository, TaskRepository
from app.db.repository.result import ReportRecord, ReportRepository
from app.generated.schemas import (
    ContractElement,
    CrossClauseConflict,
    EnhancedRiskItem,
    ReportData,
    TaskProgress,
)
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.result_service import ResultService


class ReportNotFoundError(LookupError):
    """报告不存在或尚未生成完成。"""


class ReportUnavailableError(RuntimeError):
    """报告任务失败、结果无效或下载 URL 签发失败。"""


class ReportService:
    """报告任务派发、结果落库和元数据读取。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        contracts: ContractRepository | None = None,
        tasks: TaskRepository | None = None,
        reports: ReportRepository | None = None,
    ) -> None:
        self._context = context
        self._contracts = contracts or ContractRepository(context.session)
        self._tasks = tasks or TaskRepository(context.session)
        self._reports = reports or ReportRepository(context.session)
        self._results = ResultService(context)

    async def generate(self, contract_id: UUID) -> TaskProgress:
        """组装报告输入并派发报告渲染任务。"""
        if await self._contracts.get(contract_id) is None:
            raise ContractNotFoundError(str(contract_id))

        risks = await self._results.risks(contract_id, None)
        conflicts = await self._results.conflicts(contract_id)
        elements = await self._results.elements(contract_id)
        payload = _build_report_payload(
            contract_id,
            risks=risks,
            conflicts=conflicts,
            elements=elements,
        )

        try:
            celery_task_id = self._context.task_dispatcher.send(
                TaskKind.GEN_REPORT,
                args=(str(contract_id), payload),
                task_id=None,
            )
        except Exception as exc:
            raise ReportUnavailableError(str(exc)) from exc

        task = await self._tasks.create(
            contract_id,
            TaskKind.GEN_REPORT.name,
            celery_task_id,
        )
        await self._context.session.commit()
        return TaskProgress(
            taskId=str(task.id),
            stage=task.stage,
            percent=task.progress,
            status=task.status,
            result={"contractId": str(contract_id)},
        )

    async def get(self, contract_id: UUID) -> ReportData:
        """读取报告元数据；按最新任务结果补写后签发下载 URL。"""
        if await self._contracts.get(contract_id) is None:
            raise ContractNotFoundError(str(contract_id))

        report = await self._reports.get(contract_id)
        if report is None:
            report = await self._finalize_from_latest_task(contract_id)
        return self._to_report_data(contract_id, report)

    async def _finalize_from_latest_task(self, contract_id: UUID) -> ReportRecord:
        """按最新 `GEN_REPORT` 任务结果补写报告元数据。"""
        task = await self._tasks.get_latest_by_contract(
            contract_id,
            TaskKind.GEN_REPORT.name,
        )
        if task is None:
            raise ReportNotFoundError(str(contract_id))

        snapshot = self._context.task_dispatcher.get_status(
            task.celery_task_id or str(task.id)
        )
        if snapshot is None or snapshot.status == "RUNNING":
            raise ReportNotFoundError(str(contract_id))
        if snapshot.status != "DONE":
            raise ReportUnavailableError(_snapshot_error(snapshot.result))

        storage_path = _extract_storage_path(snapshot.result)
        if not storage_path:
            raise ReportUnavailableError("报告任务缺少 storagePath")
        pdf_storage_path = _extract_optional_str(snapshot.result, "pdfStoragePath")

        report = await self._reports.create(
            contract_id,
            "DOCX",
            storage_path,
            pdf_storage_path=pdf_storage_path,
        )
        await self._context.session.commit()
        return report

    def _to_report_data(self, contract_id: UUID, report: ReportRecord) -> ReportData:
        """签发双格式下载 URL 并映射 OpenAPI ReportData。

        `downloadUrl` 保留旧语义（DOCX），`wordDownloadUrl` 与其指向同一对象；
        PDF 未生成时 `pdfDownloadUrl=None`，不返回假 URL（C72 §⑥.2/§⑥.3）。
        """
        try:
            word_url = self._context.object_storage.presign_get(
                report.storage_path,
                900,
            )
            pdf_url = (
                self._context.object_storage.presign_get(report.pdf_storage_path, 900)
                if report.pdf_storage_path
                else None
            )
        except Exception as exc:
            raise ReportUnavailableError(str(exc)) from exc
        return ReportData(
            reportId=str(report.id),
            contractId=str(contract_id),
            status=report.status,
            downloadUrl=word_url,
            wordDownloadUrl=word_url,
            pdfDownloadUrl=pdf_url,
            generatedAt=report.generated_at,
        )


def _build_report_payload(
    contract_id: UUID,
    *,
    risks: list[EnhancedRiskItem],
    conflicts: list[CrossClauseConflict],
    elements: list[ContractElement],
) -> dict[str, object]:
    """按 `4-3 §⑤.1` 组装 worker 任务载荷。"""
    return {
        "contractId": str(contract_id),
        "generatedAt": datetime.now(UTC).isoformat(),
        "risks": [risk.model_dump(mode="json", by_alias=True) for risk in risks],
        "conflicts": [
            conflict.model_dump(mode="json", by_alias=True) for conflict in conflicts
        ],
        "elements": [
            element.model_dump(mode="json", by_alias=True) for element in elements
        ],
    }


def _extract_storage_path(result: dict[str, Any] | None) -> str | None:
    """只接受任务结果顶层的非空 `storagePath`。"""
    if not isinstance(result, dict):
        return None
    value = result.get("storagePath")
    if isinstance(value, str) and value:
        return value
    return None


def _extract_optional_str(result: dict[str, Any] | None, key: str) -> str | None:
    """只接受任务结果顶层的非空字符串字段（如 `pdfStoragePath`）。"""
    if not isinstance(result, dict):
        return None
    value = result.get(key)
    if isinstance(value, str) and value:
        return value
    return None


def _snapshot_error(result: dict[str, Any] | None) -> str:
    """把失败任务结果收敛为可诊断错误信息。"""
    if isinstance(result, dict):
        warnings = result.get("warnings")
        if isinstance(warnings, list) and warnings:
            return "; ".join(str(item) for item in warnings)
        for key in ("errorMessage", "error"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return "报告任务失败"


__all__ = [
    "ReportNotFoundError",
    "ReportService",
    "ReportUnavailableError",
]
