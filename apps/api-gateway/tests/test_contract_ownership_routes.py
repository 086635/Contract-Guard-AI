"""C75-A 路由级 IDOR 验收：详情 / 任务 / SSE 均返回 404。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import get_current_user, get_db, get_service_context
from app.api.v1 import contracts as contracts_router
from app.api.v1 import tasks as tasks_router
from app.db.models import Base, User
from app.db.repository.auth import UserRecord
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.generated.schemas import Role
from app.services.context import ServiceContext
from app.services.security import JwtService


class _Storage:
    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        return f"https://minio.test/{file_key}"

    def stat_object(self, file_key: str):
        raise NotImplementedError


class _Dispatcher:
    def send(self, *args, **kwargs):
        raise NotImplementedError

    def get_status(self, task_id: str):
        return None


def _user(role: Role, username: str) -> User:
    now = datetime.now(UTC)
    return User(
        id=uuid4(),
        username=username,
        password_hash="hash",
        role=role,
        is_active=True,
        display_name=username,
        created_at=now,
        updated_at=now,
    )


def _record(user: User) -> UserRecord:
    return UserRecord(
        id=user.id,
        username=user.username,
        role=user.role,
        is_active=True,
        display_name=user.display_name,
        last_login_at=None,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def test_cross_user_contract_forbidden_404() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            owner_a = _user(Role.viewer, "route-owner-a")
            owner_b = _user(Role.viewer, "route-owner-b")
            session.add_all([owner_a, owner_b])
            await session.flush()
            contract_b = await ContractRepository(session).create(
                ContractCreate(
                    filename="owner-b.pdf",
                    file_size=1,
                    file_hash="route-owner-b-hash",
                    storage_path="contracts/route-owner-b.pdf",
                    created_by_id=owner_b.id,
                )
            )
            task_b = await TaskRepository(session).create(
                contract_b.id, "PARSE_DOC", "route-celery-b"
            )
            await session.commit()

            context = ServiceContext(
                session=session,
                object_storage=_Storage(),  # type: ignore[arg-type]
                task_dispatcher=_Dispatcher(),  # type: ignore[arg-type]
                progress_broker=object(),  # type: ignore[arg-type]
            )
            app = FastAPI()
            app.include_router(contracts_router.router, prefix="/api/v1")
            app.include_router(tasks_router.router, prefix="/api/v1")
            app.dependency_overrides[get_current_user] = lambda: _record(owner_a)
            app.dependency_overrides[get_service_context] = lambda: context

            async def override_db():
                yield session

            app.dependency_overrides[get_db] = override_db
            access_token = JwtService.from_settings().issue_access(_record(owner_a))
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                detail = await client.get(f"/api/v1/contracts/{contract_b.id}")
                assert detail.status_code == 404
                task = await client.get(f"/api/v1/tasks/{task_b.id}")
                assert task.status_code == 404
                stream = await client.get(
                    f"/api/v1/tasks/{task_b.id}/stream",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                assert stream.status_code == 404
        await engine.dispose()

    asyncio.run(scenario())


def test_admin_can_read_cross_user_contract() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            admin = _user(Role.admin, "route-admin")
            owner = _user(Role.viewer, "route-owner")
            session.add_all([admin, owner])
            await session.flush()
            contract = await ContractRepository(session).create(
                ContractCreate(
                    filename="owner.pdf",
                    file_size=1,
                    file_hash="route-owner-hash",
                    storage_path="contracts/route-owner.pdf",
                    created_by_id=owner.id,
                )
            )
            await session.commit()
            # ADMIN 例外在仓储过滤层验证：不受 owner 限制。
            fetched = await ContractRepository(session).get(
                contract.id, owner_id=None, include_all=True
            )
            assert fetched is not None
        await engine.dispose()

    asyncio.run(scenario())
