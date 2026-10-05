"""反馈 Service。

设计要点：
1. 反馈写入继续复用 C6 的 `target_type + target_id` 多态关联。
2. OpenAPI DTO 缺失字段在本模块统一投影，不散落到路由。
3. 非法枚举在 Service 层转换为稳定校验异常。
4. C38 的列表查询只按持久化列过滤，分页与计数由 Repository 完成。

对应：`文件集合/codex/4.C9配置与反馈服务.md`、`文件集合/codex/4.C38反馈列表契约与持久化.md`。
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx

from app.db.repository.config_feedback import FeedbackRecord, FeedbackRepository
from app.generated.schemas import (
    FeedbackCreateBody,
    FeedbackExportData,
    FeedbackItem,
    FeedbackVerdict,
    TargetType,
)
from app.services.context import ServiceContext


class FeedbackValidationError(ValueError):
    """反馈输入不合法。"""


class FeedbackExportError(RuntimeError):
    """反馈下载文件尚未生成。"""


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

    async def export_csv(self, actor_id: UUID) -> FeedbackExportData:
        """由管理员导出完整标注集，复用私有对象存储的短期下载地址。"""
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["反馈ID", "合同ID", "目标类型", "目标ID", "判定", "用户修正", "审核人", "是否采纳", "创建时间"])
        target_labels = {"RISK": "风险", "CONFLICT": "矛盾", "SEAL": "印章", "ELEMENT": "要素"}
        verdict_labels = {"CORRECT": "确认正确", "FALSE_POSITIVE": "误报", "MISSING": "漏报"}
        page, count = 1, 0
        while True:
            items, total = await self.list(contract_id=None, target_type=None, verdict=None, page=page, page_size=100)
            for item in items:
                target = "条款" if item.target_type == TargetType.element and item.target_id.startswith("clause-") else target_labels[item.target_type.value]
                row = [item.id, item.contract_id, target, item.target_id, verdict_labels[item.verdict.value], item.user_correction or "", item.reviewer or "", "是" if item.is_incorporated else "否", item.created_at.isoformat()]
                # 用户修正内容按文本导出，避免电子表格把它执行为公式。
                writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in row])
            count += len(items)
            if not items or count >= total:
                break
            page += 1
        key = f"feedback-exports/{datetime.now(UTC):%Y/%m}/{actor_id}/feedback-{uuid4().hex}.csv"
        try:
            upload_url = self._context.object_storage.presign_put(key, 600)
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.put(upload_url, content=buffer.getvalue().encode("utf-8-sig"), headers={"Content-Type": "text/csv; charset=utf-8"})
                response.raise_for_status()
            download_url = self._context.object_storage.presign_get(key, 900)
        except Exception as exc:
            raise FeedbackExportError("反馈导出失败，请稍后重试或检查对象存储服务。") from exc
        return FeedbackExportData(downloadUrl=download_url, recordCount=count)

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
    "FeedbackExportError",
    "FeedbackService",
    "FeedbackValidationError",
]
