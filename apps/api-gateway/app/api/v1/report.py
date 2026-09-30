"""Report 路由（审核报告生成与导出）。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status

from app.api.deps import (
    get_audit_service,
    get_current_user,
    get_service_context,
    write_audit_event,
)
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord
from app.generated.schemas import ReportGenerateResponse, ReportResponse
from app.services.audit_service import AuditService
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.report_service import (
    ReportNotFoundError,
    ReportService,
    ReportUnavailableError,
)

router = APIRouter(prefix="/report", tags=["Report"])


@router.post(
    "/{contract_id}/generate",
    response_model=ReportGenerateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_report(
    contract_id: UUID,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    body: dict = Body(default_factory=dict),
    x_request_id: str = Header(..., alias="X-Request-ID"),
    context: ServiceContext = Depends(get_service_context),
) -> ReportGenerateResponse:
    """触发报告渲染任务（异步 Worker 执行，返回 taskId）。"""
    del body
    try:
        data = await ReportService(context).generate(contract_id)
    except ContractNotFoundError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.report,
            "REPORT",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(contract_id),
            metadata={"xRequestId": x_request_id, "errorCode": "CONTRACT_NOT_FOUND"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="contract not found",
        ) from exc
    except ReportUnavailableError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.report,
            "REPORT",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(contract_id),
            metadata={"xRequestId": x_request_id, "errorCode": "REPORT_UNAVAILABLE"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    await write_audit_event(
        audit,
        request,
        AuditAction.report,
        "REPORT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(contract_id),
        metadata={"xRequestId": x_request_id, "taskId": data.task_id},
    )
    await context.session.commit()
    return build_success_response(ReportGenerateResponse, data)


@router.get(
    "/{contract_id}",
    response_model=ReportResponse,
    status_code=status.HTTP_200_OK,
)
async def get_report(
    contract_id: UUID,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: ServiceContext = Depends(get_service_context),
) -> ReportResponse:
    """获取报告元信息（含下载 URL）。"""
    try:
        data = await ReportService(context).get(contract_id)
    except (ContractNotFoundError, ReportNotFoundError) as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.export,
            "REPORT",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(contract_id),
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="report not found",
        ) from exc
    except ReportUnavailableError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.export,
            "REPORT",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(contract_id),
            metadata={"errorCode": "REPORT_UNAVAILABLE"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    await write_audit_event(
        audit,
        request,
        AuditAction.export,
        "REPORT",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(contract_id),
        metadata={"downloadType": "DOCX"},
    )
    await context.session.commit()
    return build_success_response(ReportResponse, data)