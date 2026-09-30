"""FastAPI 入口。

装配：路由（v1）、CORS、幂等中间件（X-Request-ID / X-Trace-Id）。
业务 DTO 类型来自 app.generated.schemas（由 openapi.yaml 生成，见 gen-py.sh）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_current_user
from app.api.errors import register_error_handlers
from app.api.middlewares import IdempotencyMiddleware
from app.api.v1 import agent, audit, auth, config, contracts, feedback, report, tasks
from app.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """在接收请求前执行生产配置门禁。"""
    settings.validate_production()
    yield


app = FastAPI(
    title="企业合同智能审核与风险预警系统 API",
    version="1.1.0",
    description="基于大模型的企业合同智能审核与风险预警系统（Polyglot Monorepo · api-gateway）",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(IdempotencyMiddleware)
register_error_handlers(app)


api_router = APIRouter(prefix=settings.api_v1_prefix)

api_router.include_router(auth.router)
api_router.include_router(audit.router)
api_router.include_router(contracts.router, dependencies=[Depends(get_current_user)])
api_router.include_router(agent.router, dependencies=[Depends(get_current_user)])
api_router.include_router(config.router, dependencies=[Depends(get_current_user)])
api_router.include_router(report.router, dependencies=[Depends(get_current_user)])
api_router.include_router(feedback.router, dependencies=[Depends(get_current_user)])
api_router.include_router(tasks.router, dependencies=[Depends(get_current_user)])

app.include_router(api_router)


@app.get("/health", tags=["System"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}
