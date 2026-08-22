"""Tasks 路由（长耗时异步任务进度订阅，§5.4）。

NOTE：SSOT openapi 定义了 /tasks/{taskId} 与 /tasks/{taskId}/stream（SSE），
蓝图 v1 路由树未单列该文件，此处按 SSOT 完整性补充 tasks.py
（实现阶段可并入 report.py，待定）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.get("/{task_id}")
async def get_task(task_id: UUID) -> dict:
    """长耗时任务进度轮询。"""
    raise HTTPException(status_code=501, detail="get_task 待实现")


@router.get("/{task_id}/stream")
async def stream_task(task_id: UUID) -> dict:
    """长耗时任务进度 SSE 订阅（text/event-stream）。"""
    raise HTTPException(status_code=501, detail="stream_task 待实现 (SSE)")
