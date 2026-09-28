"""Config 路由（系统级可配置规则与 Prompt 角色，§8.3）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_service_context
from app.generated.schemas import SettingsRuleResponse, SettingsRulesResponse
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.rule_service import (
    RuleNotFoundError,
    RuleService,
    RuleVersionConflictError,
)

router = APIRouter(prefix="/settings", tags=["Config"])


@router.get("/rules", response_model=SettingsRulesResponse, status_code=200)
async def list_rules(
    contract_type: str | None = None,
    context: ServiceContext = Depends(get_service_context),
) -> SettingsRulesResponse:
    """读取系统规则配置（按合同类型）。"""
    rules = await RuleService(context).list(contract_type)
    return build_success_response(SettingsRulesResponse, rules)


@router.get("/rules/{config_id}", response_model=SettingsRuleResponse, status_code=200)
async def get_rule(
    config_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> SettingsRuleResponse:
    """读取单条规则配置。"""
    try:
        rule = await RuleService(context).get(config_id)
    except RuleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="config not found",
        ) from exc
    return build_success_response(SettingsRuleResponse, rule)


@router.put("/rules/{config_id}", response_model=SettingsRuleResponse, status_code=200)
async def update_rule(
    config_id: UUID,
    body: dict,
    context: ServiceContext = Depends(get_service_context),
) -> SettingsRuleResponse:
    """更新规则配置（需带 version 乐观锁，冲突返 ERR_DATA_CONFLICT）。"""
    try:
        rule = await RuleService(context).update(config_id, body)
    except RuleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="config not found",
        ) from exc
    except RuleVersionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="version conflict",
        ) from exc
    return build_success_response(SettingsRuleResponse, rule)
