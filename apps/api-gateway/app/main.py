"""FastAPI 入口。

装配：路由（v1）、CORS、幂等中间件（X-Request-ID / X-Trace-Id）。
业务 DTO 类型来自 app.generated.schemas（由 openapi.yaml 生成，见 gen-py.sh）。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.middlewares import IdempotencyMiddleware
from app.api.v1 import agent, config, contracts, feedback, report, tasks
from app.config import settings

app = FastAPI(
    title="企业合同智能审核与风险预警系统 API",
    version="1.1.0",
    description="基于大模型的企业合同智能审核与风险预警系统（Polyglot Monorepo · api-gateway）",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(IdempotencyMiddleware)


api_router = APIRouter(prefix=settings.api_v1_prefix)

api_router.include_router(contracts.router)
api_router.include_router(agent.router)
api_router.include_router(config.router)
api_router.include_router(report.router)
api_router.include_router(feedback.router)
api_router.include_router(tasks.router)

app.include_router(api_router)


@app.get("/health", tags=["System"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}
