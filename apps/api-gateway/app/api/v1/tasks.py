"""Tasks 路由（长耗时异步任务进度订阅，§5.4）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_service_context
from app.generated.schemas import TaskProgressResponse
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.task_service import TaskNotFoundError, TaskService

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.get("/{task_id}", response_model=TaskProgressResponse, status_code=200)
async def get_task(
    task_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> TaskProgressResponse:
    """长耗时任务进度轮询。"""
    progress = await TaskService(context).get(task_id)
    if progress is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        )
    return build_success_response(TaskProgressResponse, progress)


@router.get("/{task_id}/stream")
async def stream_task(
    task_id: UUID,
    context: ServiceContext = Depends(get_service_context),
) -> StreamingResponse:
    """长耗时任务进度 SSE 订阅（text/event-stream）。"""
    service = TaskService(context)
    try:
        await service.get(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="task not found",
        ) from exc
    return StreamingResponse(
        service.stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
