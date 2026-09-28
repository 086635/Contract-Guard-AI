"""合同结果查询 Service。

设计要点：
1. 合同存在性检查统一在本 Service 完成。
2. parse-status 读取最新 `PARSE_DOC` 任务，缺失时返回 None。
3. 结果 DTO 只来自 C5 Repository 和唯一 ContractProjector。

对应：`文件集合/codex/4.C13合同结果查询服务.md`。
"""

from __future__ import annotations

from uuid import UUID

from app.db.repository.contract import ContractRepository, TaskRepository
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


class ResultService:
    """合同结果读取。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._contracts = ContractRepository(context.session)
        self._tasks = TaskRepository(context.session)
        self._elements = ElementRepository(context.session)
        self._risks = RiskRepository(context.session)
        self._seals = SealRepository(context.session)

    async def parse_status(self, contract_id: UUID) -> TaskProgress | None:
        """读取合同最新解析任务进度。"""
        await self._require_contract(contract_id)
        await IngestionService(self._context).finalize_parse(contract_id)
        task = await self._tasks.get_latest_by_contract(
            contract_id,
            TaskKind.PARSE_DOC.name,
        )
        if task is None:
            return None
        result = {"contractId": str(contract_id)}
        if task.error_message:
            result["errorMessage"] = task.error_message
        return TaskProgress(
            taskId=str(task.id),
            stage=task.stage,
            percent=task.progress,
            status=task.status,
            result=result,
        )

    async def elements(self, contract_id: UUID) -> list[ContractElement]:
        """读取切片并投影为 API element。"""
        await self._require_contract(contract_id)
        await IngestionService(self._context).finalize_parse(contract_id)
        chunks = await self._elements.list_by_contract(contract_id)
        return list(ContractProjector.elements(chunks))

    async def risks(
        self,
        contract_id: UUID,
        level: str | RiskLevel | None,
    ) -> list[EnhancedRiskItem]:
        """读取风险列表。"""
        await self._require_contract(contract_id)
        await IngestionService(self._context).finalize_audit(contract_id)
        parsed_level = RiskLevel(level.upper()) if isinstance(level, str) else level
        return await self._risks.list_by_contract(contract_id, parsed_level)

    async def conflicts(self, contract_id: UUID) -> list[CrossClauseConflict]:
        """读取跨条款矛盾。"""
        await self._require_contract(contract_id)
        await IngestionService(self._context).finalize_audit(contract_id)
        return await self._risks.list_conflicts_by_contract(contract_id)

    async def seals(self, contract_id: UUID) -> list[SealSignatureDetection]:
        """读取印章检测结果。"""
        await self._require_contract(contract_id)
        await IngestionService(self._context).finalize_parse(contract_id)
        return await self._seals.list_by_contract(contract_id)

    async def _require_contract(self, contract_id: UUID) -> None:
        if await self._contracts.get(contract_id) is None:
            raise ContractNotFoundError(str(contract_id))


__all__ = ["ResultService"]
