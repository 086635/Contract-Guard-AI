"""Contracts 路由（合同上传 / 解析 / 要素 / 风险 / 矛盾 / 印章）。

对应 data_scheme.md §5.2 级联时序、§7.3 端点清单、§8.1/§8.2/§8.4。

NOTE：Walking Skeleton：返回 MockDataFactory 统一包络数据。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from app.generated.schemas import (
    CommitBody,
    CommitResultResponse,
    ConflictListResponse,
    ContractDetailResponse,
    ContractListResponse,
    ContractUploadResponse,
    ElementListResponse,
    ParseStatusResponse,
    PresignResponse,
    RiskListResponse,
    SealListResponse,
    UploadBody,
)
from app.services.mock_factory import MockDataFactory

router = APIRouter(prefix="/contracts", tags=["Contracts"])


@router.get("", response_model=ContractListResponse, status_code=status.HTTP_200_OK)
async def list_contracts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contract_type: str | None = None,
    status: str | None = None,
) -> ContractListResponse:
    """合同列表（工作台历史，§5.1）。"""
    return MockDataFactory.list_contracts(page, page_size)


@router.post(
    "/presign", response_model=PresignResponse, status_code=status.HTTP_200_OK
)
async def presign_upload(body: dict) -> PresignResponse:
    """获取直传凭证（上传三步法 步骤1，§5.3）。"""
    return MockDataFactory.presign_upload()


@router.post(
    "/upload",
    response_model=ContractUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_contract(body: UploadBody) -> ContractUploadResponse:
    """触发解析任务（上传三步法 步骤3，§5.3）。需 X-Request-ID 幂等。"""
    return MockDataFactory.upload_contract(body)


@router.get(
    "/{contract_id}",
    response_model=ContractDetailResponse,
    status_code=status.HTTP_200_OK,
)
async def get_contract(contract_id: UUID) -> ContractDetailResponse:
    """合同详情。"""
    return MockDataFactory.get_contract(contract_id)


@router.get(
    "/{contract_id}/parse-status",
    response_model=ParseStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_parse_status(contract_id: UUID) -> ParseStatusResponse:
    """解析状态轮询（§5.2-B）。"""
    return MockDataFactory.get_parse_status(contract_id)


@router.get(
    "/{contract_id}/elements",
    response_model=ElementListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_elements(contract_id: UUID) -> ElementListResponse:
    """要素抽取（§5.2-C）。"""
    return MockDataFactory.get_elements(contract_id)


@router.get(
    "/{contract_id}/risks",
    response_model=RiskListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_risks(
    contract_id: UUID, level: str | None = None
) -> RiskListResponse:
    """风险列表（§5.2-D / §8.4 EnhancedRiskItem）。"""
    return MockDataFactory.get_risks(contract_id, level)


@router.get(
    "/{contract_id}/conflicts",
    response_model=ConflictListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_conflicts(contract_id: UUID) -> ConflictListResponse:
    """跨条款矛盾（§5.2-E / §8.2）。"""
    return MockDataFactory.get_conflicts(contract_id)


@router.get(
    "/{contract_id}/seals",
    response_model=SealListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_seals(contract_id: UUID) -> SealListResponse:
    """印章签名合规（§5.2-F / §8.1）。"""
    return MockDataFactory.get_seals(contract_id)


@router.post(
    "/{contract_id}/commit",
    response_model=CommitResultResponse,
    status_code=status.HTTP_200_OK,
)
async def commit_contract(contract_id: UUID, body: CommitBody) -> CommitResultResponse:
    """采纳修改 / 新版本（§5.2 收尾，需 version 乐观锁）。"""
    return MockDataFactory.commit_contract(contract_id, body)
