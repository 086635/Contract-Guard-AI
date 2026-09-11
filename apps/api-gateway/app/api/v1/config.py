"""Config 路由（系统级可配置规则与 Prompt 角色，§8.3）。"""

from __future__ import annotations

from fastapi import APIRouter

from app.generated.schemas import SettingsRuleResponse, SettingsRulesResponse
from app.services.mock_factory import MockDataFactory

router = APIRouter(prefix="/settings", tags=["Config"])


@router.get("/rules", response_model=SettingsRulesResponse, status_code=200)
async def list_rules(contract_type: str | None = None) -> SettingsRulesResponse:
    """读取系统规则配置（按合同类型）。"""
    return MockDataFactory.list_rules(contract_type)


@router.get("/rules/{config_id}", response_model=SettingsRuleResponse, status_code=200)
async def get_rule(config_id: str) -> SettingsRuleResponse:
    """读取单条规则配置。"""
    return MockDataFactory.get_rule(config_id)


@router.put("/rules/{config_id}", response_model=SettingsRuleResponse, status_code=200)
async def update_rule(config_id: str, body: dict) -> SettingsRuleResponse:
    """更新规则配置（需带 version 乐观锁，冲突返 ERR_DATA_CONFLICT）。"""
    return MockDataFactory.update_rule(config_id, body)
