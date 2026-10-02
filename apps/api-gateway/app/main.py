"""FastAPI 入口。

装配：路由（v1）、CORS、幂等中间件（X-Request-ID / X-Trace-Id）。
业务 DTO 类型来自 app.generated.schemas（由 openapi.yaml 生成，见 gen-py.sh）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import get_current_user
from app.api.errors import register_error_handlers
from app.api.middlewares import IdempotencyMiddleware
from app.api.v1 import (
    agent,
    audit,
    auth,
    config,
    contracts,
    feedback,
    report,
    standard_clauses,
    tasks,
)
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
    # C67：生产 overlay 置 `CG_DISABLE_DOCS=true` → 公开 docs/openapi 一律关闭
    docs_url=None if settings.disable_docs else "/docs",
    redoc_url=None if settings.disable_docs else "/redoc",
    openapi_url=None if settings.disable_docs else "/openapi.json",
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
api_router.include_router(standard_clauses.router)
api_router.include_router(contracts.router, dependencies=[Depends(get_current_user)])
api_router.include_router(agent.router, dependencies=[Depends(get_current_user)])
api_router.include_router(config.router, dependencies=[Depends(get_current_user)])
api_router.include_router(report.router, dependencies=[Depends(get_current_user)])
api_router.include_router(feedback.router, dependencies=[Depends(get_current_user)])
# tasks: 鉴权在路由内声明（SSE 用 streamToken 或 Bearer 双凭据，见 app/api/v1/tasks.py）
api_router.include_router(tasks.router)

app.include_router(api_router)


@app.get("/health", tags=["System"])
async def health() -> dict[str, str]:
    """兼容保留：进程存活（等价于 `/health/live`）。"""
    return {"status": "ok", "service": settings.app_name}


#: 单依赖探测超时（秒）：超时即视为不可用（探针本身不阻塞）。
READINESS_TIMEOUT_S = 2.0
#: readiness 依赖名（唯一处；顺序即返回体顺序）。
READINESS_DEPENDENCIES = ("database", "redis", "minio", "qdrant")


async def _check_database() -> str:
    """DB 就绪：执行 `SELECT 1`（失败由 `_probe` 折算为 error）。"""
    from sqlalchemy import text

    from app.db.session import engine

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return "ok"


async def _check_redis() -> str:
    """Redis 就绪：复用进度适配器的 `ping()`（不新增第二套客户端）。"""
    from app.services.progress import RedisProgressBroker

    return "ok" if await RedisProgressBroker().ping() else "error"


async def _check_minio() -> str:
    """MinIO 就绪：复用对象存储适配器的 `bucket_exists()`。"""
    from app.services.storage import MinioObjectStorage

    storage = MinioObjectStorage()
    return "ok" if await asyncio.to_thread(storage.bucket_exists) else "error"


async def _check_qdrant() -> str:
    """向量库就绪：复用 `rag_core` 工厂构造的适配器 `health()`（不新增客户端）。"""
    from rag_core.config import RagSettings
    from rag_core.factory import build_vector_store

    def probe() -> bool:
        store = build_vector_store(RagSettings())
        health = getattr(store, "health", None)
        # inmemory 实现没有 health()：进程内实现视为就绪（生产由 C55 门禁强制 qdrant）
        return bool(health()) if callable(health) else True

    return "ok" if await asyncio.to_thread(probe) else "error"


async def _probe(check: Callable[[], Awaitable[str]]) -> str:
    """执行单个依赖探测：异常/超时一律折算 `error`（不回显底层信息）。"""
    try:
        return await asyncio.wait_for(check(), timeout=READINESS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - 探针失败不得把异常与连接串暴露给调用方
        return "error"


@app.get("/health/live", tags=["System"])
async def health_live() -> dict[str, str]:
    """存活探针：**只表示进程活着**，不检查任何外部依赖。"""
    return {"status": "ok", "service": settings.app_name}


@app.get("/health/ready", tags=["System"])
async def health_ready(response: Response) -> dict[str, object]:
    """就绪探针：DB / Redis / MinIO / Qdrant 四项检查，任一失败 → 503。

    返回体只含依赖名与 `ok`/`error`，**不含连接串、地址与凭据**（§⑥-1）。
    """
    checks = {
        "database": await _probe(_check_database),
        "redis": await _probe(_check_redis),
        "minio": await _probe(_check_minio),
        "qdrant": await _probe(_check_qdrant),
    }
    ready = all(value == "ok" for value in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if ready else "unavailable",
        "service": settings.app_name,
        "checks": checks,
    }
