"""合同提交 Service。

设计要点：
1. 用 `X-Request-ID` 保证提交批次幂等。
2. 用 `contracts.version` 实现乐观锁。
3. 提交明细与合同版本更新在同一事务中完成。

对应：`文件集合/codex/4.C11合同提交与版本边界.md`。
"""

from __future__ import annotations

from uuid import UUID

from app.db.repository.auth import UserRecord
from app.db.repository.contract import (
    CommitItemRecord,
    CommitRepository,
    ContractRepository,
    owner_scope,
)
from app.generated.schemas import BatchResult, CommitBody, FailedItem
from app.services.context import ServiceContext

_REJECT_REASON = "用户未采纳该修改项。"


class ContractNotFoundError(LookupError):
    """合同不存在。"""


class CommitVersionConflictError(RuntimeError):
    """提交版本冲突。"""


class CommitValidationError(ValueError):
    """提交请求缺少有效明细。"""


class CommitService:
    """合同采纳批次与版本管理。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._contracts = ContractRepository(context.session)
        self._commits = CommitRepository(context.session)

    async def commit(
        self,
        contract_id: UUID,
        body: CommitBody,
        request_id: str,
        user: UserRecord | None = None,
    ) -> BatchResult:
        """提交采纳结果并返回局部成功结果。"""
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

        existing = await self._commits.list_by_request(request_id)
        if existing:
            return self._to_batch(existing)

        if body.version is not None and body.version != contract.version:
            raise CommitVersionConflictError(
                f"version {body.version} != {contract.version}"
            )

        unique_items = {item.id: item for item in body.items}
        if not unique_items:
            raise CommitValidationError("items 不能为空")

        version = contract.version
        if any(item.accept for item in unique_items.values()):
            version = await self._contracts.increment_version(contract_id)
        await self._commits.create_items(
            contract_id,
            request_id,
            version,
            [(item.id, item.accept) for item in unique_items.values()],
        )
        await self._context.session.commit()
        return self._to_batch(await self._commits.list_by_request(request_id))

    @staticmethod
    def _to_batch(records: list[CommitItemRecord]) -> BatchResult:
        return BatchResult(
            successIds=[record.item_id for record in records if record.accepted],
            failedItems=[
                FailedItem(id=record.item_id, reason=_REJECT_REASON)
                for record in records
                if not record.accepted
            ],
        )


__all__ = [
    "CommitService",
    "CommitValidationError",
    "CommitVersionConflictError",
    "ContractNotFoundError",
]
