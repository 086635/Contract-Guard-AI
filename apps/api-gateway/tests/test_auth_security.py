"""C45 认证安全原语与会话轮换测试。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from app.db.models import AuthSession
from app.db.repository.auth import (
    RefreshTokenReusedError,
    SessionRepository,
    UserCreate,
    UserRepository,
)
from app.generated.schemas import Role
from app.services.security import (
    JwtService,
    PasswordService,
    TokenValidationError,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
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


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coroutine)


def _jwt_service(now: datetime = FIXED_NOW) -> JwtService:
    return JwtService(
        secret="unit-test-secret-0123456789abcdef",
        issuer="contract-guard",
        audience="contract-guard-api",
        access_ttl_seconds=900,
        now=lambda: now,
    )


def _user_record():
    """构造最小用户投影，供 JWT 签发测试使用。"""
    from app.db.repository.auth import UserRecord

    return UserRecord(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        username="alice",
        role=Role.admin,
        is_active=True,
        display_name="Alice",
        last_login_at=None,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("Correct-Horse-Battery-Staple")
    assert hashed != "Correct-Horse-Battery-Staple"
    assert hashed.startswith("$argon2id$")
    assert verify_password("Correct-Horse-Battery-Staple", hashed) is True
    assert verify_password("wrong-password", hashed) is False
    assert verify_password("Correct-Horse-Battery-Staple", "not-a-valid-hash") is False


def test_password_service_accepts_injected_hasher() -> None:
    service = PasswordService()
    hashed = service.hash("injected")
    assert service.verify("injected", hashed) is True
    assert service.verify("not-injected", hashed) is False


def test_jwt_expiry_and_audience() -> None:
    service = _jwt_service()
    token = service.issue_access(_user_record())

    claims = service.verify_access(token)
    assert claims.sub == UUID("11111111-1111-1111-1111-111111111111")
    assert claims.role is Role.admin
    assert claims.typ == "access"
    assert claims.iss == "contract-guard"
    assert claims.aud == "contract-guard-api"

    with pytest.raises(TokenValidationError):
        _jwt_service(now=FIXED_NOW + timedelta(seconds=901)).verify_access(token)

    wrong_audience = JwtService(
        secret="unit-test-secret-0123456789abcdef",
        issuer="contract-guard",
        audience="another-api",
        access_ttl_seconds=900,
        now=lambda: FIXED_NOW,
    ).issue_access(_user_record())
    with pytest.raises(TokenValidationError):
        service.verify_access(wrong_audience)

    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(TokenValidationError):
        service.verify_access(tampered)


def test_refresh_token_is_hashed_and_random() -> None:
    raw_one = generate_refresh_token()
    raw_two = generate_refresh_token()
    hashed = hash_refresh_token(raw_one)

    assert raw_one != raw_two
    assert len(hashed) == 64
    assert hashed != raw_one
    assert hash_refresh_token(raw_one) == hashed


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C45 refresh session PostgreSQL 集成测试",
)
def test_refresh_rotation_replay_rejected() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_refresh_rotation_replay(POSTGRES_DSN))


async def _exercise_refresh_rotation_replay(dsn: str) -> None:
    schema = new_schema_name("cg_c45_rotate")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            user = await UserRepository(session).create(
                UserCreate(
                    username="rotation-user",
                    password_hash=hash_password("rotation-password"),
                    role=Role.admin,
                    display_name="Rotation User",
                )
            )
            sessions = SessionRepository(session)
            raw_old_token = generate_refresh_token()
            old_hash = hash_refresh_token(raw_old_token)
            old_session = await sessions.create(
                user.id,
                old_hash,
                FIXED_NOW + timedelta(days=7),
            )
            await session.commit()

            stored = await session.get(AuthSession, old_session.id)
            assert stored is not None
            assert stored.refresh_token_hash == old_hash
            assert stored.refresh_token_hash != raw_old_token

            new_hash = hash_refresh_token(generate_refresh_token())
            rotated = await sessions.rotate(
                old_hash,
                new_hash,
                FIXED_NOW + timedelta(days=7),
                now=FIXED_NOW,
            )
            await session.commit()

            old_record = await sessions.get_by_hash(old_hash)
            assert old_record is not None
            assert old_record.id == old_session.id
            assert old_record.revoked_at == FIXED_NOW
            assert old_record.replaced_by_id == rotated.id

            with pytest.raises(RefreshTokenReusedError):
                await sessions.rotate(
                    old_hash,
                    hash_refresh_token(generate_refresh_token()),
                    FIXED_NOW + timedelta(days=7),
                    now=FIXED_NOW + timedelta(seconds=1),
                )
            await session.commit()

            rotated_record = await sessions.get_by_hash(new_hash)
            assert rotated_record is not None
            assert rotated_record.revoked_at == FIXED_NOW + timedelta(seconds=1)
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C45 logout PostgreSQL 集成测试",
)
def test_logout_revokes_session() -> None:
    assert POSTGRES_DSN is not None
    _run(_exercise_logout_revokes_session(POSTGRES_DSN))


async def _exercise_logout_revokes_session(dsn: str) -> None:
    schema = new_schema_name("cg_c45_logout")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            user = await UserRepository(session).create(
                UserCreate(
                    username="logout-user",
                    password_hash=hash_password("logout-password"),
                    role=Role.viewer,
                    display_name="Logout User",
                )
            )
            sessions = SessionRepository(session)
            refresh_hash = hash_refresh_token(generate_refresh_token())
            created = await sessions.create(
                user.id,
                refresh_hash,
                FIXED_NOW + timedelta(days=7),
            )
            await session.commit()

            assert await sessions.revoke(created.id, now=FIXED_NOW) is True
            await session.commit()
            revoked = await sessions.get_by_hash(refresh_hash)
            assert revoked is not None
            assert revoked.revoked_at == FIXED_NOW

            assert await sessions.revoke(created.id, now=FIXED_NOW + timedelta(seconds=1)) is True
            await session.commit()
            revoked_again = await sessions.get_by_hash(refresh_hash)
            assert revoked_again is not None
            assert revoked_again.revoked_at == FIXED_NOW
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)