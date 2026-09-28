"""交付模块 C6：配置与反馈仓储。

设计要点：
1. 规则与反馈分别封装为独立仓储，避免路由层直接操作 ORM。
2. 反馈严格复用 `TargetType` / `FeedbackVerdict`，不另造枚举字符串。
3. 返回表级持久化 DTO，完整 OpenAPI 配置/反馈 DTO 聚合留给服务层。

对应：`文件集合/codex/4.C6配置与反馈DAL.md`。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
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


class RuleRecord(_PersistenceDto):
    """规则表持久化投影。"""

    id: UUID
    rule_name: str
    category: str
    prompt_template: str
    threshold: float | None
    is_enabled: bool


class FeedbackRecord(_PersistenceDto):
    """反馈表持久化投影。"""

    id: UUID
    target_type: TargetType
    target_id: str
    verdict: FeedbackVerdict
    user_comment: str | None
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
    ) -> FeedbackRecord:
        """创建反馈并校验契约枚举。"""
        try:
            normalized_target = TargetType(target_type)
        except ValueError as exc:
            raise ValueError(f"非法 target_type: {target_type}") from exc
        try:
            normalized_verdict = FeedbackVerdict(verdict)
        except ValueError as exc:
            raise ValueError(f"非法 verdict: {verdict}") from exc

        record = UserFeedback(
            target_type=normalized_target,
            target_id=target_id,
            verdict=normalized_verdict,
            user_comment=user_comment,
        )
        self._session.add(record)
        await self._session.flush()
        return FeedbackRecord.model_validate(record)

    async def list(
        self,
        target_type: TargetType | None = None,
    ) -> list[FeedbackRecord]:
        """按可选目标类型列出反馈。"""
        statement = select(UserFeedback).order_by(
            UserFeedback.created_at.desc(),
            UserFeedback.id.asc(),
        )
        if target_type is not None:
            statement = statement.where(UserFeedback.target_type == target_type)

        records = (await self._session.scalars(statement)).all()
        return [FeedbackRecord.model_validate(record) for record in records]


__all__ = [
    "FeedbackRecord",
    "FeedbackRepository",
    "RuleRecord",
    "RuleRepository",
    "RuleUpsert",
]
