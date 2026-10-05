"""合同结果查询 Service。

设计要点：
1. 合同存在性检查统一在本 Service 完成。
2. parse-status 读取最新 `PARSE_DOC` 任务，缺失时返回 None。
3. 结果 DTO 只来自 C5 Repository 和唯一 ContractProjector。

对应：`文件集合/codex/4.C13合同结果查询服务.md`。
"""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from app.db.repository.auth import UserRecord
from app.db.repository.contract import ContractRepository, TaskRepository, owner_scope
from app.db.repository.result import (
    ElementRepository,
    RiskRepository,
    SealRepository,
)
from app.generated.schemas import (
    ContractElement,
    CrossClauseConflict,
    EnhancedRiskItem,
    RiskLevel,
    SealSignatureDetection,
    TaskProgress,
)
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.ingestion import IngestionService
from app.services.projector import ContractProjector
from app.services.task_service import TaskService


def _audit_notice(payload: dict[str, Any], status: str) -> str:
    warnings = payload.get("warnings") or []
    stages = {item.get("stage"): item.get("status") for item in payload.get("stageLog") or [] if isinstance(item, dict)}
    recovered = status in {"DONE", "DEGRADED"} and all(stages.get(stage) == "OK" for stage in ("classify", "extract", "risk", "conflict"))
    notices = [item for item in warnings if isinstance(item, str) and item and not (recovered and item.startswith(("[function_calling]", "[json_object]")))]
    fallback = "审核正在运行" if status == "RUNNING" else "审核结果已返回，请结合原文人工复核。"
    return notices[0][:300] if notices else fallback


class ResultService:
    """合同结果读取。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._contracts = ContractRepository(context.session)
        self._tasks = TaskRepository(context.session)
        self._elements = ElementRepository(context.session)
        self._risks = RiskRepository(context.session)
        self._seals = SealRepository(context.session)

    async def parse_status(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> TaskProgress | None:
        """读取合同最新解析任务进度。"""
        await self._require_contract(contract_id, user)
        await IngestionService(self._context).finalize_parse(contract_id)
        task = await self._tasks.get_latest_by_contract(
            contract_id,
            TaskKind.PARSE_DOC.name,
        )
        if task is None:
            scan = await self._tasks.get_latest_by_contract(contract_id, TaskKind.SCAN_UPLOAD.name)
            if scan is None:
                return None
            scan_progress = await TaskService(self._context).get(scan.id)
            if scan_progress is None:
                return None
            scan_payload = scan_progress.result or {}
            return scan_progress.model_copy(update={"result": {"contractId": str(contract_id), "errorMessage": scan_payload.get("reason") or scan.error_message or "文件安全扫描中"}})
        progress = await TaskService(self._context).get(task.id)
        result: dict[str, Any] = {"contractId": str(contract_id)}
        error_message = task.error_message
        if progress is not None and isinstance(progress.result, dict):
            fresh_error = progress.result.get("errorMessage")
            if isinstance(fresh_error, str) and fresh_error:
                error_message = fresh_error
            elif progress.status.value == "FAILED":
                warnings = progress.result.get("warnings")
                if isinstance(warnings, list):
                    error_message = next((value for value in warnings if isinstance(value, str) and value), error_message)
        if error_message:
            result["errorMessage"] = error_message
        if progress is not None and isinstance(progress.result, dict):
            payload = progress.result
            clauses = payload.get("clauses")
            if isinstance(clauses, list):
                result["clauseCount"] = len(clauses)
                result["parseOutcome"] = payload.get("status")
                for key in ("pageCount", "lineCount", "charCount"):
                    value = payload.get(key)
                    if isinstance(value, int) and value >= 0:
                        result[key] = value
                if progress.status.value == "DONE":
                    result["auditStatus"] = "NOT_STARTED"
                    result["auditMessage"] = "尚未识别可审核条款，请检查原文编号或人工复核。"
                    audit = await self._tasks.get_latest_by_contract(contract_id, TaskKind.RUN_AUDIT.name) if clauses else None
                    if audit is not None:
                        audit_progress = await TaskService(self._context).get(audit.id)
                        if audit_progress is not None:
                            result["auditStatus"] = audit_progress.status.value
                            result["auditTaskId"] = str(audit.id)
                            audit_payload = audit_progress.result or {}
                            if audit_payload.get("status") == "DEGRADED":
                                result["auditStatus"] = "DEGRADED"
                            result["auditStage"] = audit_progress.stage
                            result["auditPercent"] = audit_progress.percent
                            health = audit_payload.get("healthScore")
                            score = health.get("overall") if isinstance(health, dict) else None
                            if (
                                audit_progress.status.value == "DONE"
                                and result["auditStatus"] in {"DONE", "DEGRADED"}
                                and isinstance(score, (int, float))
                                and not isinstance(score, bool)
                                and math.isfinite(score)
                                and 0 <= score <= 100
                            ):
                                result["auditScore"] = score
                            model_info = audit_payload.get("modelInfo")
                            if isinstance(model_info, dict):
                                provider = model_info.get("provider")
                                model_name = model_info.get("model")
                                if isinstance(provider, str) and provider in {"offline", "ollama", "openai", "deepseek"}:
                                    result["auditProvider"] = provider
                                if isinstance(model_name, str) and model_name:
                                    result["auditModel"] = model_name[:100]
                            result["auditMessage"] = _audit_notice(audit_payload, result["auditStatus"])
                            if result["auditStatus"] in {"FAILED", "DEGRADED"}:
                                from ai_engine.llm.settings import get_llm_settings
                                if result.get("auditProvider", get_llm_settings().provider) == "offline":
                                    result["auditMessage"] = "当前使用离线模型，自动风险审核未完成；请人工复核或配置真实模型后重试。"
        return TaskProgress(
            taskId=str(task.id),
            stage=progress.stage if progress is not None else task.stage,
            percent=progress.percent if progress is not None else task.progress,
            status=progress.status if progress is not None else task.status,
            result=result,
        )

    async def elements(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> list[ContractElement]:
        """读取切片并投影为 API element。"""
        await self._require_contract(contract_id, user)
        await IngestionService(self._context).finalize_parse(contract_id)
        chunks = await self._elements.list_by_contract(contract_id)
        return list(ContractProjector.elements(chunks))

    async def risks(
        self,
        contract_id: UUID,
        level: str | RiskLevel | None,
        user: UserRecord | None = None,
    ) -> list[EnhancedRiskItem]:
        """读取风险列表。"""
        await self._require_contract(contract_id, user)
        await IngestionService(self._context).finalize_audit(contract_id)
        parsed_level = RiskLevel(level.upper()) if isinstance(level, str) else level
        return await self._risks.list_by_contract(contract_id, parsed_level)

    async def conflicts(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> list[CrossClauseConflict]:
        """读取跨条款矛盾。"""
        await self._require_contract(contract_id, user)
        await IngestionService(self._context).finalize_audit(contract_id)
        return await self._risks.list_conflicts_by_contract(contract_id)

    async def seals(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> list[SealSignatureDetection]:
        """读取印章检测结果。"""
        await self._require_contract(contract_id, user)
        await IngestionService(self._context).finalize_parse(contract_id)
        return await self._seals.list_by_contract(contract_id)

    async def _require_contract(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> None:
        owner_id, include_all = owner_scope(user)
        if owner_id is None:
            contract = await self._contracts.get(contract_id)
        else:
            contract = await self._contracts.get(
                contract_id,
                owner_id=owner_id,
                include_all=include_all,
            )
        if contract is None:
            raise ContractNotFoundError(str(contract_id))


__all__ = ["ResultService"]
