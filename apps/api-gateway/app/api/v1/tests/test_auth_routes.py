"""C47 认证与 RBAC 路由测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Coroutine
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    session_factory,
)

from app.api.deps import get_current_user, get_db
from app.api.v1 import auth as auth_router
from app.db.repository.auth import UserRecord
from app.generated.schemas import Role
from app.services.auth_service import AuthService

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
ALLOWED_ORIGIN = "http://localhost:5173"


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


def _build_app(session) -> FastAPI:
    """构造只装配 auth/admin 路由的测试应用，并复用测试 session。"""

    app = FastAPI()

    async def override_get_db() -> AsyncIterator:
        yield session

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router.router, prefix="/api/v1")
    return app


async def _create_user(
    session,
    username: str,
    password: str,
    role: Role,
) -> UserRecord:
    service = AuthService(session)
    user = await service.create_user(username, password, role, username)
    await session.commit()
    return user


def _refresh_cookie(response) -> str:
    token = response.cookies.get("cg_refresh")
    assert token, response.headers.get("set-cookie")
    return token


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C47 认证路由 PostgreSQL 集成测试",
)
def test_auth_routes_login_refresh_logout() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_login_refresh_logout(POSTGRES_DSN))


async def _exercise_login_refresh_logout(dsn: str) -> None:
    schema = new_schema_name("cg_c47_auth")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            admin = await _create_user(session, "route-admin", "route-password", Role.admin)
            app = _build_app(session)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "route-admin", "password": "route-password"},
                )
                assert login.status_code == 200
                login_data = login.json()["data"]
                assert login_data["user"]["id"] == str(admin.id)
                assert login_data["accessToken"]
                assert "cg_refresh=" in login.headers["set-cookie"]
                assert "HttpOnly" in login.headers["set-cookie"]
                assert "Secure" in login.headers["set-cookie"]
                assert "SameSite=lax" in login.headers["set-cookie"]
                assert "Path=/api/v1/auth" in login.headers["set-cookie"]
                access_token = login_data["accessToken"]
                refresh_token = _refresh_cookie(login)

                me = await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                assert me.status_code == 200
                assert me.json()["data"]["username"] == "route-admin"

                users = await client.get(
                    "/api/v1/admin/users",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                assert users.status_code == 200
                assert users.json()["data"]["items"][0]["username"] == "route-admin"

                refreshed = await client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Origin": ALLOWED_ORIGIN,
                        "Cookie": f"cg_refresh={refresh_token}",
                    },
                )
                assert refreshed.status_code == 200
                new_access_token = refreshed.json()["data"]["accessToken"]
                new_refresh_token = _refresh_cookie(refreshed)
                assert new_refresh_token != refresh_token

                logout = await client.post(
                    "/api/v1/auth/logout",
                    headers={
                        "Authorization": f"Bearer {new_access_token}",
                        "Origin": ALLOWED_ORIGIN,
                        "Cookie": f"cg_refresh={new_refresh_token}",
                    },
                )
                assert logout.status_code == 200
                assert logout.json()["data"]["revoked"] is True
                assert "Max-Age=0" in logout.headers["set-cookie"]

                after_logout = await client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Origin": ALLOWED_ORIGIN,
                        "Cookie": f"cg_refresh={new_refresh_token}",
                    },
                )
                assert after_logout.status_code == 401
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C47 Bearer 依赖 PostgreSQL 集成测试",
)
def test_bearer_required() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_bearer_required(POSTGRES_DSN))


async def _exercise_bearer_required(dsn: str) -> None:
    schema = new_schema_name("cg_c47_bearer")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            app = _build_app(session)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/auth/me")
                assert response.status_code == 401
                assert response.headers.get("www-authenticate") == "Bearer"
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C47 RBAC PostgreSQL 集成测试",
)
def test_role_dependency_403() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_role_dependency_403(POSTGRES_DSN))


async def _exercise_role_dependency_403(dsn: str) -> None:
    schema = new_schema_name("cg_c47_rbac")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            viewer = await _create_user(session, "route-viewer", "viewer-password", Role.viewer)
            app = _build_app(session)
            app.dependency_overrides[get_current_user] = lambda: viewer
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/api/v1/admin/users")
                assert response.status_code == 403

                create_response = await client.post(
                    "/api/v1/admin/users",
                    headers={"Origin": ALLOWED_ORIGIN},
                    json={
                        "username": "new-user",
                        "password": "new-password",
                        "role": "VIEWER",
                        "displayName": "New User",
                    },
                )
                assert create_response.status_code == 403

                update_response = await client.patch(
                    f"/api/v1/admin/users/{uuid4()}",
                    headers={"Origin": ALLOWED_ORIGIN},
                    json={"isActive": False},
                )
                assert update_response.status_code == 403
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C47 Origin 校验 PostgreSQL 集成测试",
)
def test_refresh_cookie_flags() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_cookie_and_origin(POSTGRES_DSN))


async def _exercise_cookie_and_origin(dsn: str) -> None:
    schema = new_schema_name("cg_c47_origin")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            await _create_user(session, "origin-user", "origin-password", Role.viewer)
            app = _build_app(session)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "origin-user", "password": "origin-password"},
                )
                assert login.status_code == 200
                refresh_token = _refresh_cookie(login)

                refreshed = await client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Origin": ALLOWED_ORIGIN,
                        "Cookie": f"cg_refresh={refresh_token}",
                    },
                )
                assert refreshed.status_code == 200
                assert "HttpOnly" in refreshed.headers["set-cookie"]
                assert "Secure" in refreshed.headers["set-cookie"]
                assert "SameSite=lax" in refreshed.headers["set-cookie"]
                assert "Path=/api/v1/auth" in refreshed.headers["set-cookie"]
                refresh_token = _refresh_cookie(refreshed)

                missing_origin = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Cookie": f"cg_refresh={refresh_token}"},
                )
                assert missing_origin.status_code == 403

                blocked_origin = await client.post(
                    "/api/v1/auth/refresh",
                    headers={
                        "Origin": "https://evil.example",
                        "Cookie": f"cg_refresh={refresh_token}",
                    },
                )
                assert blocked_origin.status_code == 403
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)