"""Feedback 路由（标注反馈，数据飞轮，§9 FeedbackItem）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_service_context
from app.generated.schemas import FeedbackCreateBody, FeedbackResponse
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.feedback_service import FeedbackService, FeedbackValidationError

router = APIRouter(prefix="/feedback", tags=["Feedback"])


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
