"""Config 路由（系统级可配置规则与 Prompt 角色，§8.3）。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/settings", tags=["Config"])


@router.get("/rules")
async def list_rules(contract_type: str | None = None) -> dict:
    """读取系统规则配置（按合同类型）。"""
    raise HTTPException(status_code=501, detail="list_rules 待实现")


@router.get("/rules/{config_id}")
async def get_rule(config_id: str) -> dict:
    """读取单条规则配置。"""
    raise HTTPException(status_code=501, detail="get_rule 待实现")


@router.put("/rules/{config_id}")
async def update_rule(config_id: str, body: dict) -> dict:
    """更新规则配置（需带 version 乐观锁，冲突返 ERR_DATA_CONFLICT）。"""
    raise HTTPException(status_code=501, detail="update_rule 待实现")
