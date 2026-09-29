"""反馈 Service。

设计要点：
1. 反馈写入继续复用 C6 的 `target_type + target_id` 多态关联。
2. OpenAPI DTO 缺失字段在本模块统一投影，不散落到路由。
3. 非法枚举在 Service 层转换为稳定校验异常。
4. C38 的列表查询只按持久化列过滤，分页与计数由 Repository 完成。

对应：`文件集合/codex/4.C9配置与反馈服务.md`、`文件集合/codex/4.C38反馈列表契约与持久化.md`。
"""

from __future__ import annotations

from uuid import UUID

from app.db.repository.config_feedback import FeedbackRecord, FeedbackRepository
from app.generated.schemas import (
    FeedbackCreateBody,
    FeedbackItem,
    FeedbackVerdict,
    TargetType,
)
from app.services.context import ServiceContext


class FeedbackValidationError(ValueError):
    """反馈输入不合法。"""


class FeedbackService:
    """反馈创建、列表与投影。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._repository = FeedbackRepository(context.session)

    async def create(self, body: FeedbackCreateBody) -> FeedbackItem:
        """创建并返回反馈。"""
        try:
            contract_id = UUID(body.contract_id)
            record = await self._repository.create(
                body.target_type,
                body.target_id,
                body.verdict,
                body.user_correction,
                contract_id=contract_id,
                reviewer=body.reviewer,
            )
        except (ValueError, TypeError) as exc:
            raise FeedbackValidationError(str(exc)) from exc
        await self._context.session.commit()
        return self._to_dto(record)

    async def list(
        self,
        *,
        contract_id: UUID | None,
        target_type: TargetType | None,
        verdict: FeedbackVerdict | None,
        page: int,
        page_size: int,
    ) -> tuple[list[FeedbackItem], int]:
        """按可选维度分页查询反馈并投影为 OpenAPI DTO。"""
        records, total = await self._repository.list(
            target_type,
            contract_id=contract_id,
            verdict=verdict,
            page=page,
            page_size=page_size,
        )
        return [self._to_dto(record) for record in records], total

    @staticmethod
    def _to_dto(record: FeedbackRecord) -> FeedbackItem:
        return FeedbackItem(
            id=str(record.id),
            contractId=str(record.contract_id),
            targetType=record.target_type,
            targetId=record.target_id,
            verdict=record.verdict,
            userCorrection=record.user_comment,
            reviewer=record.reviewer,
            isIncorporated=False,
            createdAt=record.created_at,
        )


__all__ = [
    "FeedbackService",
    "FeedbackValidationError",
]
