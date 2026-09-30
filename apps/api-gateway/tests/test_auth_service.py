"""C46 AuthService 登录、锁定、刷新和账号策略测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.db.repository.auth import UserRepository
from app.generated.schemas import Role
from app.services.auth_service import (
    AccountLockedError,
    AccountNotFoundError,
    AuthService,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    LastAdminProtectedError,
)
from app.services.security import JwtService, verify_password
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    session_factory,
)

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
FIXED_NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


class _Clock:
    """可手动推进的 UTC 时钟。"""

    def __init__(self, current: datetime = FIXED_NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


def _jwt_service(clock: _Clock) -> JwtService:
    return JwtService(
        secret="service-test-secret-0123456789abcdef",
        issuer="contract-guard",
        audience="contract-guard-api",
        access_ttl_seconds=900,
        now=clock.now,
    )


def _service(
    session,
    clock: _Clock,
    *,
    lockout_threshold: int = 5,
    lockout_minutes: int = 15,
) -> AuthService:
    return AuthService(
        session,
        jwt_service=_jwt_service(clock),
        now=clock.now,
        lockout_threshold=lockout_threshold,
        lockout_minutes=lockout_minutes,
        refresh_ttl_seconds=604800,
    )


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C46 AuthService PostgreSQL 集成测试",
)
def test_login_success_and_failure() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_login_success_and_failure(POSTGRES_DSN))


async def _exercise_login_success_and_failure(dsn: str) -> None:
    schema = new_schema_name("cg_c46_login")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        clock = _Clock()
        async with factory() as session:
            service = _service(session, clock)
            created = await service.create_user(
                "alice",
                "correct-password",
                Role.admin,
                "Alice",
            )
            await session.commit()

            bundle = await service.login("alice", "correct-password")
            assert bundle.access_token
            assert bundle.refresh_token
            assert bundle.expires_in == 900
            assert bundle.refresh_expires_in == 604800
            assert bundle.session_id
            assert bundle.user.id == created.id

            claims = _jwt_service(clock).verify_access(bundle.access_token)
            assert claims.sub == created.id
            await session.commit()

            with pytest.raises(InvalidCredentialsError):
                await service.login("missing-user", "wrong-password")

            with pytest.raises(InvalidCredentialsError):
                await service.login("alice", "wrong-password")
            await session.commit()

            auth_record = await UserRepository(session).get_auth_by_username("alice")
            assert auth_record is not None
            assert auth_record.failed_login_attempts == 1

            reset_bundle = await service.login("ALICE", "correct-password")
            await session.commit()
            assert reset_bundle.access_token
            auth_record = await UserRepository(session).get_auth_by_username("alice")
            assert auth_record is not None
            assert auth_record.failed_login_attempts == 0
            assert auth_record.locked_until is None
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C46 AuthService lockout PostgreSQL 集成测试",
)
def test_login_lockout_window() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_login_lockout_window(POSTGRES_DSN))


async def _exercise_login_lockout_window(dsn: str) -> None:
    schema = new_schema_name("cg_c46_lockout")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        clock = _Clock()
        async with factory() as session:
            service = _service(session, clock)
            await service.create_user(
                "locked-user",
                "correct-password",
                Role.viewer,
                "Locked User",
            )
            await session.commit()

            for _ in range(4):
                with pytest.raises(InvalidCredentialsError):
                    await service.login("locked-user", "wrong-password")
                await session.commit()

            with pytest.raises(AccountLockedError) as locked:
                await service.login("locked-user", "wrong-password")
            await session.commit()
            assert locked.value.retry_after_seconds == 900

            with pytest.raises(AccountLockedError):
                await service.login("locked-user", "correct-password")

            clock.advance(minutes=15)
            bundle = await service.login("locked-user", "correct-password")
            await session.commit()
            assert bundle.access_token

            auth_record = await UserRepository(session).get_auth_by_username(
                "locked-user"
            )
            assert auth_record is not None
            assert auth_record.failed_login_attempts == 0
            assert auth_record.locked_until is None
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C46 AuthService 管理员保护 PostgreSQL 集成测试",
)
def test_last_admin_protected() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_last_admin_protected(POSTGRES_DSN))


async def _exercise_last_admin_protected(dsn: str) -> None:
    schema = new_schema_name("cg_c46_admin")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        clock = _Clock()
        async with factory() as session:
            service = _service(session, clock)
            first = await service.create_user(
                "admin-one",
                "admin-password",
                Role.admin,
                "Admin One",
            )
            await session.commit()

            with pytest.raises(LastAdminProtectedError):
                await service.update_user(first.id, is_active=False)
            await session.rollback()

            second = await service.create_user(
                "admin-two",
                "admin-password",
                Role.admin,
                "Admin Two",
            )
            await session.commit()

            disabled = await service.update_user(first.id, is_active=False)
            await session.commit()
            assert disabled.is_active is False

            with pytest.raises(LastAdminProtectedError):
                await service.update_user(second.id, role=Role.viewer)
            await session.rollback()
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C46 AuthService refresh/logout PostgreSQL 集成测试",
)
def test_refresh_uses_session_repository() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_refresh_and_logout(POSTGRES_DSN))


async def _exercise_refresh_and_logout(dsn: str) -> None:
    schema = new_schema_name("cg_c46_refresh")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        clock = _Clock()
        async with factory() as session:
            service = _service(session, clock)
            user = await service.create_user(
                "refresh-user",
                "refresh-password",
                Role.viewer,
                "Refresh User",
            )
            await session.commit()

            first = await service.login("refresh-user", "refresh-password")
            await session.commit()

            second = await service.refresh(first.refresh_token)
            await session.commit()
            assert second.refresh_token != first.refresh_token
            assert second.session_id != first.session_id

            with pytest.raises(InvalidRefreshTokenError):
                await service.refresh(first.refresh_token)

            with pytest.raises(InvalidRefreshTokenError):
                await service.refresh("")

            with pytest.raises(InvalidRefreshTokenError):
                await service.refresh(second.refresh_token)

            third = await service.login("refresh-user", "refresh-password")
            await session.commit()
            await service.logout(user.id, third.session_id)
            await session.commit()
            with pytest.raises(InvalidRefreshTokenError):
                await service.refresh(third.refresh_token)

            other = await service.create_user(
                "other-user",
                "other-password",
                Role.viewer,
                "Other User",
            )
            await session.commit()
            with pytest.raises(InvalidRefreshTokenError):
                await service.logout(other.id, third.session_id)
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C46 AuthService 创建用户 PostgreSQL 集成测试",
)
def test_create_user_hashes_password() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_create_user_hashes_password(POSTGRES_DSN))


async def _exercise_create_user_hashes_password(dsn: str) -> None:
    schema = new_schema_name("cg_c46_create")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            service = _service(session, _Clock())
            created = await service.create_user(
                "new-user",
                "new-password",
                Role.reviewer,
                "New User",
            )
            await session.commit()

            record = await UserRepository(session).get_auth_by_username("new-user")
            assert record is not None
            assert record.id == created.id
            assert record.password_hash != "new-password"
            assert record.password_hash.startswith("$argon2id$")
            assert verify_password("new-password", record.password_hash) is True

            with pytest.raises(AccountNotFoundError):
                await service.me(UUID("00000000-0000-0000-0000-000000000000"))
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)