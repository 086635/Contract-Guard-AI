"""Feedback 路由（标注反馈，数据飞轮，§9 FeedbackItem）。

NOTE：Walking Skeleton：返回 MockDataFactory 统一包络数据。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.generated.schemas import FeedbackCreateBody, FeedbackResponse
from app.services.mock_factory import MockDataFactory

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post("", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def create_feedback(body: FeedbackCreateBody) -> FeedbackResponse:
    """标注反馈（正确/误报/漏报 + 用户修正 Ground Truth）。需 X-Request-ID 幂等。"""
    return MockDataFactory.create_feedback(body)
