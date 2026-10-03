"""C64 SSE stream token 与订阅鉴权测试。

离线：令牌绑定/过期/篡改/类型与任务作用域、路由挂载方式（Bearer vs streamToken）。
真实 PostgreSQL：`POST /tasks/{id}/stream-token` → `GET /stream?streamToken=` 全链路（DSN 门禁）。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_service_context
from app.api.v1 import tasks as tasks_router
from app.db.repository.auth import UserRecord
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.generated.schemas import ContractStatus, Role, StreamTokenData
from app.services.context import ServiceContext
from app.services.security import (
    STREAM_TOKEN_TTL_SECONDS,
    JwtService,
    TokenValidationError,
)
from app.services.sse import SSE_MEDIA_TYPE
from app.services.task_service import (
    StreamTokenScopeError,
    TaskNotFoundError,
    TaskService,
)
from rag_contracts.enums import ContractType
from tests.service_testkit import (
    create_schema,
    drop_schema,
    install_test_auth,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)
from tests.test_api_real_endpoints import _dependency_calls

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = REPO_ROOT / "packages" / "schema" / "openapi.yaml"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeSession:
    async def commit(self) -> None:
        return None


class _TaskRow:
    """最小任务属性替身（TaskService 只读字段）。"""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class FakeTaskRepository:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = {row["id"]: _TaskRow(**row) for row in rows}

    async def get(self, task_id: UUID) -> Any:
        return self.rows.get(task_id)


class FakeContractRepository:
    """归属测试替身：存在由路由/任务层显式控制，此处只返回非空合同。"""

    async def get(self, contract_id: UUID, **kwargs: Any) -> Any:
        return _TaskRow(id=contract_id)


def _user(user_id: UUID | None = None, *, username: str = "u1") -> UserRecord:
    now = datetime.now(UTC)
    return UserRecord(
        id=user_id or uuid4(),
        username=username,
        role=Role.viewer,
        is_active=True,
        display_name="C64 User",
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


def _jwt(*, now: datetime = T0) -> JwtService:
    return JwtService(
        secret="c64-secret-0123456789abcdef0123456789abcdef", now=lambda: now
    )


def _service(
    *,
    jwt: JwtService,
    task_ids: list[UUID] | None = None,
) -> TaskService:
    rows = [
        {
            "id": task_id,
            "contract_id": uuid4(),
            "task_type": "PARSE_DOC",
            "stage": "queued",
            "progress": 0.0,
            "status": "RUNNING",
            "celery_task_id": "celery-x",
            "error_message": None,
        }
        for task_id in (task_ids or [])
    ]
    context = ServiceContext(
        session=cast(AsyncSession, FakeSession()),
        object_storage=cast(Any, object()),
        task_dispatcher=cast(Any, object()),
        progress_broker=cast(Any, object()),
    )
    return TaskService(
        context,
        jwt=jwt,
        tasks=cast(Any, FakeTaskRepository(rows)),
        contracts=cast(Any, FakeContractRepository()),
    )


# --------------------------------------------------------------------------
# 断言 1 / 3：合法 token 建立 SSE（作用域绑定 userId + taskId）
# --------------------------------------------------------------------------
def test_stream_token_scoped() -> None:
    """令牌绑定 (userId, taskId)：本任务可校验通过，长度 60 秒且 claims 完整。"""
    task_id = uuid4()
    user = _user()
    jwt = _jwt()
    service = _service(jwt=jwt, task_ids=[task_id])

    data = asyncio.run(service.issue_stream_token(task_id, user))

    assert isinstance(data, StreamTokenData)
    assert data.expires_in == STREAM_TOKEN_TTL_SECONDS == 60
    claims = service.verify_stream_token(task_id, data.stream_token)
    assert claims.sub == user.id
    assert claims.task_id == task_id
    assert claims.typ == "stream" and claims.scope == "stream"
    assert claims.exp - claims.iat == timedelta(seconds=60)


def test_stream_token_missing_task_is_not_found() -> None:
    """签发前先校验任务存在：未知任务 → TaskNotFoundError（路由映射 404，不泄露存在性）。"""
    service = _service(jwt=_jwt(), task_ids=[])

    with pytest.raises(TaskNotFoundError):
        asyncio.run(service.issue_stream_token(uuid4(), _user()))


# --------------------------------------------------------------------------
# 断言 2：过期 / 篡改 token 拒绝
# --------------------------------------------------------------------------
def test_stream_token_expired() -> None:
    """超过 60 秒后校验失败（过期即拒）。"""
    task_id = uuid4()
    issued = _jwt()
    token, _ = issued.issue_stream(user_id=uuid4(), task_id=task_id)

    later = _jwt(now=T0 + timedelta(seconds=STREAM_TOKEN_TTL_SECONDS + 1))
    with pytest.raises(TokenValidationError):
        later.verify_stream(token)

    # 边界内（59 秒）仍然有效
    within = _jwt(now=T0 + timedelta(seconds=STREAM_TOKEN_TTL_SECONDS - 1))
    assert within.verify_stream(token).task_id == task_id


def test_stream_token_tampered_rejected() -> None:
    """签名被改（篡改 payload）→ 校验失败。"""
    task_id = uuid4()
    token, _ = _jwt().issue_stream(user_id=uuid4(), task_id=task_id)
    head, payload, signature = token.split(".")
    tampered = f"{head}.{payload}.{'A' * len(signature)}"

    with pytest.raises(TokenValidationError):
        _jwt().verify_stream(tampered)


# --------------------------------------------------------------------------
# 断言 3：跨任务（其他用户的任务）拒绝
# --------------------------------------------------------------------------
def test_cross_user_stream_forbidden() -> None:
    """令牌作用域绑定：为任务 A 签发的令牌不得订阅任务 B。

    说明（本仓库口径）：单租户私有化部署下合同**没有归属字段**（母文档 `4-7 §⑤.11`
    要求 `contracts.created_by_id`，目前尚未落库），因此「同一租户内他人合同」不可判定；
    这里验证可强制执行的边界：令牌绑定 `(userId, taskId)`，跨任务复用一律 403。
    """
    task_a, task_b = uuid4(), uuid4()
    user_a = _user(username="owner-a")
    jwt = _jwt()
    service = _service(jwt=jwt, task_ids=[task_a])
    token, _ = jwt.issue_stream(user_id=user_a.id, task_id=task_a)

    assert service.verify_stream_token(task_a, token).sub == user_a.id

    with pytest.raises(StreamTokenScopeError):
        service.verify_stream_token(task_b, token)


# --------------------------------------------------------------------------
# 断言 4：access token 不能代替 stream token
# --------------------------------------------------------------------------
def test_access_token_not_accepted_as_stream() -> None:
    """access token（typ=access、无 taskId/scope）不能当 stream token 用。"""
    jwt = _jwt()
    access = jwt.issue_access(_user())

    with pytest.raises(TokenValidationError):
        jwt.verify_stream(access)

    service = _service(jwt=jwt, task_ids=[uuid4()])
    with pytest.raises(TokenValidationError):
        service.verify_stream_token(uuid4(), access)


# --------------------------------------------------------------------------
# 路由挂载：签发要 Bearer，SSE 走双凭据；SSE 媒体类型不变
# --------------------------------------------------------------------------
def _routes() -> dict[str, APIRoute]:
    return {
        route.path: route
        for route in tasks_router.router.routes
        if isinstance(route, APIRoute)
    }


def test_stream_token_route_wiring() -> None:
    """签发路由必须挂 `get_current_user`；SSE 路由走 `stream_principal` 而不是 Bearer。"""
    routes = _routes()
    issue = routes["/tasks/{task_id}/stream-token"]
    stream = routes["/tasks/{task_id}/stream"]

    assert get_current_user in _dependency_calls(issue.dependant)
    assert tasks_router.stream_principal in _dependency_calls(stream.dependant)
    assert get_current_user not in _dependency_calls(stream.dependant)
    assert SSE_MEDIA_TYPE == "text/event-stream"


def test_openapi_declares_stream_token_path_and_dto() -> None:
    """契约面：新增 stream-token 路径与 DTO，且 SSE 路径声明 streamToken 查询参数。"""
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    assert "/tasks/{taskId}/stream-token:" in text
    assert "StreamTokenData:" in text
    assert "name: streamToken" in text


# --------------------------------------------------------------------------
# 真实 PostgreSQL + HTTP：签名 → 订阅
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C64 SSE 订阅 PostgreSQL 集成测试",
)
def test_stream_subscription_with_token_http() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_stream_http(POSTGRES_DSN))


async def _exercise_stream_http(dsn: str) -> None:
    schema = new_schema_name("cg_c64")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract = await ContractRepository(session).create(
                ContractCreate(
                    filename="c64.pdf",
                    file_size=1024,
                    file_hash="hash-c64",
                    storage_path="contracts/2026/10/c64.pdf",
                    status=ContractStatus.parsing,
                    contract_type=ContractType.OTHER,
                )
            )
            task = await TaskRepository(session).create(
                contract.id, "PARSE_DOC", "celery-c64"
            )
            other = await TaskRepository(session).create(
                contract.id, "PARSE_DOC", "celery-c64-other"
            )
            await session.commit()

            app = FastAPI()
            app.include_router(tasks_router.router, prefix="/api/v1")
            install_test_auth(app, session)
            app.dependency_overrides[get_service_context] = lambda: service_context(
                session,
                task_dispatcher=cast(Any, _DoneDispatcher()),
            )
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                issued = await client.post(f"/api/v1/tasks/{task.id}/stream-token")
                assert issued.status_code == 200, issued.text
                token = issued.json()["data"]["streamToken"]
                assert issued.json()["data"]["expiresIn"] == 60

                stream = await client.get(
                    f"/api/v1/tasks/{task.id}/stream",
                    params={"streamToken": token},
                )
                assert stream.status_code == 200, stream.text
                assert stream.headers["content-type"].startswith("text/event-stream")
                assert "event: progress" in stream.text

                # 同一令牌订阅**另一个任务** → 403（作用域绑定）
                crossed = await client.get(
                    f"/api/v1/tasks/{other.id}/stream",
                    params={"streamToken": token},
                )
                assert crossed.status_code == 403

                # access token 放查询串 → 401（类型不符）
                access = install_test_auth_token(app, session)
                denied = await client.get(
                    f"/api/v1/tasks/{task.id}/stream",
                    params={"streamToken": access},
                )
                assert denied.status_code == 401
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


class _DoneDispatcher:
    """任务快照替身：直接返回 DONE，SSE 立即结束（便于断言响应头）。"""

    def send(self, *args: object, **kwargs: object) -> str:
        raise NotImplementedError

    def get_status(self, task_id: str) -> Any:
        from app.services.ports import TaskStatusSnapshot

        return TaskStatusSnapshot(
            status="DONE",
            stage="done",
            percent=1.0,
            result={"contractId": task_id},
        )


def install_test_auth_token(app: FastAPI, session: AsyncSession) -> str:
    """为 PG 集成用例签发一个 access token（用于「不能当 stream token 用」的断言）。"""
    from app.api.deps import get_current_user as _get_current_user

    user = app.dependency_overrides[_get_current_user]()
    return JwtService.from_settings().issue_access(user)
