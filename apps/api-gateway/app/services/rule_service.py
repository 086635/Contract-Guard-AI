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
_PROJECTION_VERSION = 1
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

        if body.version != _PROJECTION_VERSION:
            raise RuleVersionConflictError(
                f"version {body.version} != {_PROJECTION_VERSION}"
            )

        review_thresholds = _validate_review_thresholds(body.review_thresholds)
        updated = await self._repository.upsert(
            RuleUpsert(
                id=rule_id,
                rule_name=body.prompt_config.role_name,
                category=body.contract_type.value,
                prompt_template=body.prompt_config.system_prompt,
                threshold=existing.threshold,
                is_enabled=body.enabled,
                review_thresholds=review_thresholds.model_dump(),
            )
        )
        await self._context.session.commit()
        return self._to_dto(updated)

    @staticmethod
    def _to_dto(record: RuleRecord) -> SystemAuditConfiguration:
        try:
            contract_type = ContractType(record.category)
        except ValueError:
            contract_type = ContractType.other
        return SystemAuditConfiguration(
            id=str(record.id),
            contractType=contract_type,
            promptConfig=PromptRoleConfig(
                roleName=record.rule_name,
                systemPrompt=record.prompt_template,
                temperature=0.0,
                riskToleranceLevel=RiskTolerance.balanced,
            ),
            enabledRules=[record.rule_name] if record.is_enabled else [],
            enabled=record.is_enabled,
            reviewThresholds=_parse_review_thresholds(record.review_thresholds),
            customEntities=[],
            updatedAt=_PROJECTION_UPDATED_AT,
            version=_PROJECTION_VERSION,
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
