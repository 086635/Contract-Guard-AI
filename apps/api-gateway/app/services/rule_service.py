"""规则配置 Service。

设计要点：
1. 路由只处理 HTTP；表记录到 OpenAPI DTO 的投影集中在本模块。
2. 当前表缺少 `updated_at` / `version`，投影采用固定值和恒定版本。
3. 版本冲突与不存在分别使用稳定异常类型。

对应：`文件集合/codex/4.C9配置与反馈服务.md`。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.db.repository.config_feedback import RuleRecord, RuleRepository, RuleUpsert
from app.generated.schemas import (
    ContractType,
    PromptRoleConfig,
    RiskTolerance,
    SystemAuditConfiguration,
)
from app.services.context import ServiceContext

_PROJECTION_UPDATED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_PROJECTION_VERSION = 1


class RuleNotFoundError(LookupError):
    """规则不存在。"""


class RuleVersionConflictError(RuntimeError):
    """规则版本冲突。"""


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
        body: dict[str, Any],
    ) -> SystemAuditConfiguration:
        """更新规则并返回投影 DTO。"""
        existing = await self._repository.get(rule_id)
        if existing is None:
            raise RuleNotFoundError(str(rule_id))

        version = body.get("version")
        if version is not None and int(version) != _PROJECTION_VERSION:
            raise RuleVersionConflictError(
                f"version {version} != {_PROJECTION_VERSION}"
            )

        prompt_config = body.get("promptConfig")
        if not isinstance(prompt_config, dict):
            prompt_config = {}
        contract_type_raw = body.get("contractType")
        category = (
            str(contract_type_raw)
            if contract_type_raw is not None
            else existing.category
        )
        updated = await self._repository.upsert(
            RuleUpsert(
                id=rule_id,
                rule_name=str(prompt_config.get("roleName", existing.rule_name)),
                category=category,
                prompt_template=str(
                    prompt_config.get(
                        "systemPrompt",
                        existing.prompt_template,
                    )
                ),
                threshold=existing.threshold,
                is_enabled=bool(body.get("enabledRules", [existing.is_enabled])),
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
            customEntities=[],
            updatedAt=_PROJECTION_UPDATED_AT,
            version=_PROJECTION_VERSION,
        )


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
    "RuleVersionConflictError",
]
