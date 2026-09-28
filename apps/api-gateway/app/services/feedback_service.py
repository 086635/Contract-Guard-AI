"""反馈 Service。

设计要点：
1. 反馈写入继续复用 C6 的 `target_type + target_id` 多态关联。
2. OpenAPI DTO 缺失字段在本模块统一投影，不散落到路由。
3. 非法枚举在 Service 层转换为稳定校验异常。

对应：`文件集合/codex/4.C9配置与反馈服务.md`。
"""

from __future__ import annotations

from app.db.repository.config_feedback import FeedbackRecord, FeedbackRepository
from app.generated.schemas import FeedbackCreateBody, FeedbackItem
from app.services.context import ServiceContext


class FeedbackValidationError(ValueError):
    """反馈枚举不合法。"""


class FeedbackService:
    """反馈创建与投影。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._repository = FeedbackRepository(context.session)

    async def create(self, body: FeedbackCreateBody) -> FeedbackItem:
        """创建并返回反馈。"""
        try:
            record = await self._repository.create(
                target_type=body.target_type,
                target_id=body.target_id,
                verdict=body.verdict,
                user_comment=body.user_correction,
            )
        except ValueError as exc:
            raise FeedbackValidationError(str(exc)) from exc
        await self._context.session.commit()
        return self._to_dto(record, body)

    @staticmethod
    def _to_dto(
        record: FeedbackRecord,
        body: FeedbackCreateBody,
    ) -> FeedbackItem:
        return FeedbackItem(
            id=str(record.id),
            contractId=body.contract_id,
            targetType=record.target_type,
            targetId=record.target_id,
            verdict=record.verdict,
            userCorrection=record.user_comment,
            reviewer=body.reviewer,
            isIncorporated=False,
            createdAt=record.created_at,
        )


__all__ = [
    "FeedbackService",
    "FeedbackValidationError",
]
