"""Feedback 路由（标注反馈与列表，数据飞轮，§9 FeedbackItem）。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    get_audit_service,
    get_service_context,
    require_roles,
    write_audit_event,
)
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord
from app.generated.schemas import (
    FeedbackCreateBody,
    FeedbackExportResponse,
    FeedbackListData,
    FeedbackListResponse,
    FeedbackResponse,
    FeedbackVerdict,
    Role,
    TargetType,
)
from app.services.audit_service import AuditService
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.feedback_service import (
    FeedbackExportError,
    FeedbackService,
    FeedbackValidationError,
)

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post("/export", response_model=FeedbackExportResponse)
async def export_feedback(
    request: Request,
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: ServiceContext = Depends(get_service_context),
) -> FeedbackExportResponse:
    """管理员导出完整标注集，文件和下载地址由服务器实际生成。"""
    try:
        data = await FeedbackService(context).export_csv(user.id)
    except FeedbackExportError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await write_audit_event(audit, request, AuditAction.export, "FEEDBACK_CSV", AuditOutcome.success, actor_id=user.id, actor_role=user.role, metadata={"recordCount": data.record_count})
    await context.session.commit()
    return build_success_response(FeedbackExportResponse, data)


@router.get("", response_model=FeedbackListResponse, status_code=status.HTTP_200_OK)
async def list_feedback(
    contract_id: UUID | None = Query(None, alias="contractId"),
    target_type: TargetType | None = Query(None, alias="targetType"),
    verdict: FeedbackVerdict | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
    context: ServiceContext = Depends(get_service_context),
) -> FeedbackListResponse:
    """分页查询反馈，支持合同、目标类型和判定过滤。"""
    items, total = await FeedbackService(context).list(
        contract_id=contract_id,
        target_type=target_type,
        verdict=verdict,
        page=page,
        page_size=page_size,
    )
    data = FeedbackListData(
        items=items,
        total=total,
        page=page,
        pageSize=page_size,
        hasMore=page * page_size < total,
    )
    return build_success_response(FeedbackListResponse, data)


@router.post("", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    body: FeedbackCreateBody,
    context: ServiceContext = Depends(get_service_context),
) -> FeedbackResponse:
    """标注反馈（正确/误报/漏报 + 用户修正 Ground Truth）。需 X-Request-ID 幂等。"""
    try:
        feedback = await FeedbackService(context).create(body)
    except FeedbackValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return build_success_response(FeedbackResponse, feedback)
