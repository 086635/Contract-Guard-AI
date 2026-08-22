"""Report 路由（审核报告生成与导出）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/report", tags=["Report"])


@router.post("/{contract_id}/generate")
async def generate_report(contract_id: UUID, body: dict) -> dict:
    """触发报告渲染任务（异步 Worker 执行，返回 taskId）。"""
    raise HTTPException(status_code=501, detail="generate_report 待实现")


@router.get("/{contract_id}")
async def get_report(contract_id: UUID) -> dict:
    """获取报告元信息（含下载 URL）。"""
    raise HTTPException(status_code=501, detail="get_report 待实现")
