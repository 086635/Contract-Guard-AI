"""交付模块 C6：配置与反馈仓储。

设计要点：
1. 规则与反馈分别封装为独立仓储，避免路由层直接操作 ORM。
2. 反馈严格复用 `TargetType` / `FeedbackVerdict`，不另造枚举字符串。
3. 返回表级持久化 DTO，完整 OpenAPI 配置/反馈 DTO 聚合留给服务层。

对应：`文件集合/codex/4.C6配置与反馈DAL.md`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SettingsRule, UserFeedback
from app.generated.schemas import FeedbackVerdict, TargetType


class _PersistenceDto(BaseModel):
    """仓储层 DTO 基类。"""

    model_config = ConfigDict(from_attributes=True)


class RuleUpsert(_PersistenceDto):
    """创建或更新规则所需字段。"""

    id: UUID | None = None
    rule_name: str
    category: str
    prompt_template: str
    threshold: float | None = None
    is_enabled: bool = True
    review_thresholds: dict[str, float]
    editor_state: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuleRecord(_PersistenceDto):
    """规则表持久化投影。"""

    id: UUID
    rule_name: str
    category: str
    prompt_template: str
    threshold: float | None
    is_enabled: bool
    review_thresholds: dict[str, float]
    editor_state: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    updated_at: datetime | None = None


class FeedbackRecord(_PersistenceDto):
    """反馈表持久化投影。"""

    id: UUID
    contract_id: UUID
    target_type: TargetType
    target_id: str
    verdict: FeedbackVerdict
    user_comment: str | None
    reviewer: str | None
    created_at: datetime


class RuleRepository:
    """审计规则仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, category: str | None = None) -> list[RuleRecord]:
        """按可选分类列出规则。"""
        statement = select(SettingsRule).order_by(
            SettingsRule.rule_name.asc(),
            SettingsRule.id.asc(),
        )
        if category is not None:
            statement = statement.where(SettingsRule.category == category)

        records = (await self._session.scalars(statement)).all()
        return [RuleRecord.model_validate(record) for record in records]

    async def get(self, rule_id: UUID) -> RuleRecord | None:
        """按主键读取规则。"""
        record = await self._session.get(SettingsRule, rule_id)
        if record is None:
            return None
        return RuleRecord.model_validate(record)

    async def upsert(self, rule: RuleUpsert) -> RuleRecord:
        """按主键 insert-or-update 规则。"""
        values = rule.model_dump(exclude={"id"})
        values["id"] = rule.id or uuid4()
        statement = pg_insert(SettingsRule).values(**values)
        update_columns = {
            column.name: getattr(statement.excluded, column.name)
            for column in SettingsRule.__table__.columns
            if column.name != "id"
        }
        statement = statement.on_conflict_do_update(
            index_elements=[SettingsRule.id],
            set_=update_columns,
        ).returning(SettingsRule)
        record = (await self._session.scalars(statement)).one()
        return RuleRecord.model_validate(record)

    async def create(self, rule: RuleUpsert) -> RuleRecord:
        """Only insert: duplicate IDs/names must never overwrite another rule."""
        record = SettingsRule(**rule.model_dump(exclude={"id"}), id=rule.id or uuid4())
        self._session.add(record)
        await self._session.flush()
        return RuleRecord.model_validate(record)

    async def update_if_version(self, rule: RuleUpsert, expected: int) -> RuleRecord | None:
        """Atomic compare-and-update, including concurrent requests."""
        values = rule.model_dump(exclude={"id"})
        values["version"] = expected + 1
        values["updated_at"] = datetime.now(UTC)
        statement = (
            update(SettingsRule)
            .where(SettingsRule.id == rule.id, SettingsRule.version == expected)
            .values(**values).returning(SettingsRule)
            .execution_options(populate_existing=True)
        )
        record = await self._session.scalar(statement)
        return RuleRecord.model_validate(record) if record is not None else None


class FeedbackRepository:
    """用户反馈仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        target_type: TargetType | str,
        target_id: str,
        verdict: FeedbackVerdict | str,
        user_comment: str | None,
        *,
        contract_id: UUID,
        reviewer: str | None = None,
    ) -> FeedbackRecord:
        """创建反馈并校验契约枚举与合同关联。"""
        try:
            normalized_target = TargetType(target_type)
        except ValueError as exc:
            raise ValueError(f"非法 target_type: {target_type}") from exc
        try:
            normalized_verdict = FeedbackVerdict(verdict)
        except ValueError as exc:
            raise ValueError(f"非法 verdict: {verdict}") from exc

        record = UserFeedback(
            contract_id=contract_id,
            target_type=normalized_target,
            target_id=target_id,
            verdict=normalized_verdict,
            user_comment=user_comment,
            reviewer=reviewer,
        )
        self._session.add(record)
        await self._session.flush()
        return FeedbackRecord.model_validate(record)

    async def list(
        self,
        target_type: TargetType | str | None = None,
        *,
        contract_id: UUID | None = None,
        verdict: FeedbackVerdict | str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[FeedbackRecord], int]:
        """按合同/目标类型/判定过滤并分页，返回稳定排序记录与总数。"""
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size 必须在 1~100")

        filters = []
        if contract_id is not None:
            filters.append(UserFeedback.contract_id == contract_id)
        if target_type is not None:
            filters.append(UserFeedback.target_type == TargetType(target_type))
        if verdict is not None:
            filters.append(UserFeedback.verdict == FeedbackVerdict(verdict))

        total_statement = select(func.count()).select_from(UserFeedback).where(*filters)
        total = int((await self._session.scalar(total_statement)) or 0)
        statement = (
            select(UserFeedback)
            .where(*filters)
            .order_by(UserFeedback.created_at.desc(), UserFeedback.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        records = (await self._session.scalars(statement)).all()
        return [FeedbackRecord.model_validate(record) for record in records], total

__all__ = [
    "FeedbackRecord",
    "FeedbackRepository",
    "RuleRecord",
    "RuleRepository",
    "RuleUpsert",
]
