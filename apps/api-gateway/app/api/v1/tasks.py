"""Tasks 路由（长耗时异步任务进度订阅，§5.4）。

NOTE：Walking Skeleton：返回 MockDataFactory 统一包络数据。
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.generated.schemas import TaskProgressResponse
from app.services.mock_factory import MockDataFactory

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.get("/{task_id}", response_model=TaskProgressResponse, status_code=200)
async def get_task(task_id: UUID) -> TaskProgressResponse:
    """长耗时任务进度轮询。"""
    return MockDataFactory.get_task(task_id)


@router.get("/{task_id}/stream")
async def stream_task(task_id: UUID):
    """长耗时任务进度 SSE 订阅（text/event-stream）。"""
    async def event_generator():
        task = MockDataFactory.get_task(task_id)
        yield f"data: {json.dumps(task.model_dump(by_alias=True))}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
