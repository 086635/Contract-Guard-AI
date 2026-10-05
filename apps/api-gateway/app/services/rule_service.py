"""规则配置 Service。

设计要点：
1. 路由只处理 HTTP；表记录到 OpenAPI DTO 的投影集中在本模块。
2. `enabled` 复用 `is_enabled`，阈值以 JSON 持久化。
3. 版本冲突与不存在分别使用稳定异常类型。

对应：`文件集合/codex/4.C9配置与反馈服务.md`、`文件集合/codex/4.C39设置契约与阈值持久化.md`。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.db.repository.config_feedback import RuleRecord, RuleRepository, RuleUpsert
from app.generated.schemas import (
    ContractType,
    PromptRoleConfig,
    ReviewThresholds,
    RiskTolerance,
    SystemAuditConfiguration,
)
from app.services.context import ServiceContext

_PROJECTION_UPDATED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_DEFAULT_REVIEW_THRESHOLDS = {
    "high": 0.85,
    "medium": 0.65,
    "low": 0.40,
}


class RuleNotFoundError(LookupError):
    """规则不存在。"""


class RuleVersionConflictError(RuntimeError):
    """规则版本冲突。"""


class RuleThresholdValidationError(ValueError):
    """复核阈值不合法。"""


class RuleService:
    """规则读取、投影与更新。"""

    def __init__(self, context: ServiceContext) -> None:
        self._context = context
        self._repository = RuleRepository(context.session)

    async def list(
        self,
        contract_type: str | None,
    ) -> list[SystemAuditConfiguration]:
        """列出规则。"""
        category = _normalize_category(contract_type)
        records = await self._repository.list(category)
        return [self._to_dto(record) for record in records]

    async def get(self, rule_id: UUID) -> SystemAuditConfiguration:
        """读取单条规则。"""
        record = await self._repository.get(rule_id)
        if record is None:
            raise RuleNotFoundError(str(rule_id))
        return self._to_dto(record)

    async def update(
        self,
        rule_id: UUID,
        body: SystemAuditConfiguration,
    ) -> SystemAuditConfiguration:
        """更新规则并返回投影 DTO。"""
        existing = await self._repository.get(rule_id)
        if existing is None:
            raise RuleNotFoundError(str(rule_id))

        if body.version != existing.version:
            raise RuleVersionConflictError(
                f"version {body.version} != {existing.version}"
            )

        try:
            updated = await self._repository.update_if_version(
                self._upsert(body, rule_id, existing.threshold), existing.version
            )
        except IntegrityError as exc:
            await self._context.session.rollback()
            raise RuleVersionConflictError("规则名称已存在") from exc
        if updated is None:
            raise RuleVersionConflictError("配置已被其他操作更新，请重新加载")
        await self._context.session.commit()
        return self._to_dto(updated)

    async def create(self, body: SystemAuditConfiguration) -> SystemAuditConfiguration:
        """Create a real editable rule without silently upserting."""
        try:
            rule_id = UUID(body.id)
        except ValueError as exc:
            raise RuleThresholdValidationError("规则 ID 必须是 UUID") from exc
        try:
            created = await self._repository.create(self._upsert(body, rule_id))
        except IntegrityError as exc:
            await self._context.session.rollback()
            raise RuleVersionConflictError("规则名称或 ID 已存在") from exc
        await self._context.session.commit()
        return self._to_dto(created)

    @staticmethod
    def _upsert(body: SystemAuditConfiguration, rule_id: UUID, threshold: float | None = None) -> RuleUpsert:
        if not body.prompt_config.role_name.strip() or not body.prompt_config.system_prompt.strip():
            raise RuleThresholdValidationError("规则名称与审查说明不能为空")
        return RuleUpsert(
            id=rule_id, rule_name=body.prompt_config.role_name.strip(),
            category=body.contract_type.value, prompt_template=body.prompt_config.system_prompt,
            threshold=threshold, is_enabled=body.enabled,
            review_thresholds=_validate_review_thresholds(body.review_thresholds).model_dump(),
            editor_state={
                "promptConfig": body.prompt_config.model_dump(mode="json", by_alias=True),
                "enabledRules": body.enabled_rules, "customEntities": [entity.model_dump(mode="json", by_alias=True) for entity in body.custom_entities],
            },
        )

    @staticmethod
    def _to_dto(record: RuleRecord) -> SystemAuditConfiguration:
        try:
            contract_type = ContractType(record.category)
        except ValueError:
            contract_type = ContractType.other
        return SystemAuditConfiguration(
            id=str(record.id),
            contractType=contract_type,
            promptConfig=PromptRoleConfig.model_validate(record.editor_state["promptConfig"]) if record.editor_state.get("promptConfig") else PromptRoleConfig(
                roleName=record.rule_name,
                systemPrompt=record.prompt_template,
                temperature=0.0,
                riskToleranceLevel=RiskTolerance.balanced,
            ),
            enabledRules=record.editor_state.get("enabledRules", [record.rule_name] if record.is_enabled else []),
            enabled=record.is_enabled,
            reviewThresholds=_parse_review_thresholds(record.review_thresholds),
            customEntities=record.editor_state.get("customEntities", []),
            updatedAt=record.updated_at or _PROJECTION_UPDATED_AT,
            version=record.version,
        )


def _parse_review_thresholds(raw: dict[str, float] | None) -> ReviewThresholds:
    """从持久化 JSON 还原阈值；脏数据显式报错。"""
    try:
        return ReviewThresholds.model_validate(raw or _DEFAULT_REVIEW_THRESHOLDS)
    except ValidationError as exc:
        raise RuleThresholdValidationError("reviewThresholds 非法") from exc


def _validate_review_thresholds(value: ReviewThresholds) -> ReviewThresholds:
    """校验 0~1、有限数和 high ≥ medium ≥ low。"""
    thresholds = (value.high, value.medium, value.low)
    if any(not math.isfinite(item) for item in thresholds):
        raise RuleThresholdValidationError("reviewThresholds 必须是有限数")
    if any(item < 0.0 or item > 1.0 for item in thresholds):
        raise RuleThresholdValidationError("reviewThresholds 必须在 0~1")
    if not (value.high >= value.medium >= value.low):
        raise RuleThresholdValidationError(
            "reviewThresholds 必须满足 high ≥ medium ≥ low"
        )
    return value


def _normalize_category(contract_type: str | None) -> str | None:
    if contract_type is None:
        return None
    try:
        return ContractType(contract_type).value
    except ValueError:
        return contract_type


__all__ = [
    "RuleNotFoundError",
    "RuleService",
    "RuleThresholdValidationError",
    "RuleVersionConflictError",
]
