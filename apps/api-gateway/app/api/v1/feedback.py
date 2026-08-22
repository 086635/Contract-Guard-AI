"""Feedback 路由（标注反馈，数据飞轮，§9 FeedbackItem）。

NOTE：SSOT openapi 定义了 /feedback 端点，蓝图 v1 路由树未单列该文件，
此处按 SSOT 完整性补充 feedback.py（实现阶段可并入 contracts.py，待定）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/feedback", tags=["Feedback"])


@router.post("")
async def create_feedback(body: dict) -> dict:
    """标注反馈（正确/误报/漏报 + 用户修正 Ground Truth）。需 X-Request-ID 幂等。"""
    raise HTTPException(status_code=501, detail="create_feedback 待实现")
