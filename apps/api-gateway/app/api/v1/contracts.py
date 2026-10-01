"""Contracts 路由（合同上传 / 解析 / 要素 / 风险 / 矛盾 / 印章）。

对应 data_scheme.md §5.2 级联时序、§7.3 端点清单、§8.1/§8.2/§8.4。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.api.deps import (
    get_audit_service,
    get_current_user,
    get_service_context,
    write_audit_event,
)
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord
from app.generated.schemas import (
    CommitBody,
    CommitResultResponse,
    ConflictListResponse,
    ContractDetailResponse,
    ContractListResponse,
    ContractUploadResponse,
    Data,
    ElementListResponse,
    ParseStatusResponse,
    PresignResponse,
    RiskListResponse,
    SealListResponse,
    UploadBody,
)
from app.services.audit_service import AuditService
from app.services.commit_service import (
    CommitService,
    CommitValidationError,
    CommitVersionConflictError,
    ContractNotFoundError,
)
from app.services.context import ServiceContext
from app.services.contract_service import ContractService, PresignRequest
from app.services.envelope import build_success_response
from app.services.result_service import ResultService
from app.services.storage import UploadObjectError

router = APIRouter(prefix="/contracts", tags=["Contracts"])


@router.get("", response_model=ContractListResponse, status_code=status.HTTP_200_OK)
async def list_contracts(
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contract_type: str | None = None,
    status: str | None = None,
    context: ServiceContext = Depends(get_service_context),
) -> ContractListResponse:
    """合同列表（工作台历史，§5.1）。"""
    try:
        items, total = await ContractService(context).list(
            page,
            page_size,
            contract_type=contract_type,
            status=status,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    data = Data(
        items=items,
        total=total,
        page=page,
        pageSize=page_size,
        hasMore=page * page_size < total,
    )
    await write_audit_event(
        audit,
        request,
        AuditAction.view,
        "CONTRACT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        metadata={"method": "GET", "path": "/api/v1/contracts", "count": total},
    )
    await context.session.commit()
    return build_success_response(ContractListResponse, data)


@router.post("/presign", response_model=PresignResponse, status_code=status.HTTP_200_OK)
async def presign_upload(
    body: PresignRequest,
    context: ServiceContext = Depends(get_service_context),
) -> PresignResponse:
    """获取直传凭证（上传三步法 步骤1，§5.3）。含服务端准入校验（C56）。"""
    try:
        data = await ContractService(context).presign_upload(body)
    except UploadObjectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return build_success_response(PresignResponse, data)


@router.post(
    "/upload",
    response_model=ContractUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_contract(
    body: UploadBody,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: ServiceContext = Depends(get_service_context),
) -> ContractUploadResponse:
    """触发解析任务（上传三步法 步骤3，§5.3）。需 X-Request-ID 幂等。含对象复核（C56）。"""
    try:
        data = await ContractService(context).upload(body)
    except UploadObjectError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await write_audit_event(
        audit,
        request,
        AuditAction.upload,
        "CONTRACT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=data.task_id,
        metadata={"fileType": body.file_type.value},
    )
    await context.session.commit()
    return build_success_response(ContractUploadResponse, data)


@router.get(
    "/{contract_id}",
    response_model=ContractDetailResponse,
    status_code=status.HTTP_200_OK,
)
async def get_contract(
    contract_id: UUID,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: ServiceContext = Depends(get_service_context),
) -> ContractDetailResponse:
    """合同详情。"""
    contract = await ContractService(context).get(contract_id)
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        )
    await write_audit_event(
        audit,
        request,
        AuditAction.view,
        "CONTRACT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(contract_id),
        metadata={"method": "GET", "path": f"/api/v1/contracts/{contract_id}"},
    )
    await context.session.commit()
    return build_success_response(ContractDetailResponse, contract)


@router.get(
    "/{contract_id}/parse-status",
    response_model=ParseStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def get_parse_status(
    contract_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> ParseStatusResponse:
    """解析状态轮询（§5.2-B）。"""
    try:
        progress = await ResultService(context).parse_status(contract_id)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="parse task not found",
        )
    return build_success_response(ParseStatusResponse, progress)


@router.get(
    "/{contract_id}/elements",
    response_model=ElementListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_elements(
    contract_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> ElementListResponse:
    """要素抽取（§5.2-C）。"""
    try:
        elements = await ResultService(context).elements(contract_id)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    return build_success_response(ElementListResponse, elements)


@router.get(
    "/{contract_id}/risks",
    response_model=RiskListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_risks(
    contract_id: UUID,
    level: str | None = None,
    context: ServiceContext = Depends(get_service_context),
) -> RiskListResponse:
    """风险列表（§5.2-D / §8.4 EnhancedRiskItem）。"""
    try:
        risks = await ResultService(context).risks(contract_id, level)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    return build_success_response(RiskListResponse, risks)


@router.get(
    "/{contract_id}/conflicts",
    response_model=ConflictListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_conflicts(
    contract_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> ConflictListResponse:
    """跨条款矛盾（§5.2-E / §8.2）。"""
    try:
        conflicts = await ResultService(context).conflicts(contract_id)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    return build_success_response(ConflictListResponse, conflicts)


@router.get(
    "/{contract_id}/seals",
    response_model=SealListResponse,
    status_code=status.HTTP_200_OK,
)
async def get_seals(
    contract_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> SealListResponse:
    """印章签名合规（§5.2-F / §8.1）。"""
    try:
        seals = await ResultService(context).seals(contract_id)
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    return build_success_response(SealListResponse, seals)


@router.post(
    "/{contract_id}/commit",
    response_model=CommitResultResponse,
    status_code=status.HTTP_200_OK,
)
async def commit_contract(
    contract_id: UUID,
    body: CommitBody,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    x_request_id: str = Header(..., alias="X-Request-ID"),
    context: ServiceContext = Depends(get_service_context),
) -> CommitResultResponse:
    """采纳修改 / 新版本（§5.2 收尾，需 version 乐观锁）。"""
    try:
        result = await CommitService(context).commit(
            contract_id,
            body,
            x_request_id,
        )
    except ContractNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    except CommitVersionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="version conflict",
        ) from exc
    except CommitValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    await write_audit_event(
        audit,
        request,
        AuditAction.commit,
        "CONTRACT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(contract_id),
        metadata={"xRequestId": x_request_id, "version": body.version},
    )
    await context.session.commit()
    return build_success_response(CommitResultResponse, result)
