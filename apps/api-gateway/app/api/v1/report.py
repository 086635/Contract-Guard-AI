"""Report 路由（审核报告生成与导出）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.generated.schemas import ReportGenerateResponse, ReportResponse
from app.services.mock_factory import MockDataFactory

router = APIRouter(prefix="/report", tags=["Report"])


@router.post(
    "/{contract_id}/generate",
    response_model=ReportGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_report(contract_id: UUID, body: dict) -> ReportGenerateResponse:
    """触发报告渲染任务（异步 Worker 执行，返回 taskId）。"""
    return MockDataFactory.generate_report(contract_id, body)


@router.get(
    "/{contract_id}",
    response_model=ReportResponse,
    status_code=status.HTTP_200_OK,
)
async def get_report(contract_id: UUID) -> ReportResponse:
    """获取报告元信息（含下载 URL）。"""
    return MockDataFactory.get_report(contract_id)
