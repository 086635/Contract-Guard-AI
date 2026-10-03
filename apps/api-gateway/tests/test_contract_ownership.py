"""C75-A 合同归属与迁移收口测试。

覆盖：
1. Alembic 兼容加法迁移（0014 → 0015）与历史合同 legacy 标记；
2. 上传写入 owner；
3. 普通用户跨归属读取 404，ADMIN 例外；
4. 生成物包含 TASK_RECOVERED / FILE_REJECTED。
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, User
from app.db.repository.auth import UserRecord
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
    owner_scope,
)
from app.generated.schemas import FileType, Role, UploadBody
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.contract_service import ContractService, generate_file_key
from app.services.ports import StoredObjectInfo
from app.services.preview_service import PreviewService
from app.services.task_service import TaskNotFoundError, TaskService

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "apps" / "api-gateway" / "alembic.ini"


def _offline_sql(action: str, revision: str) -> str:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://cg:cg@localhost/cg")
    buffer = StringIO()
    with redirect_stdout(buffer):
        if action == "upgrade":
            command.upgrade(config, revision, sql=True)
        else:
            command.downgrade(config, revision, sql=True)
    return buffer.getvalue()


def _sqlite_sql(sql: str) -> str:
    """SQLite 不执行 FK constraint DDL；列/索引/数据迁移仍全部执行。"""
    import re

    return re.sub(
        r"ALTER TABLE contracts (?:ADD|DROP) CONSTRAINT .*?;\n?",
        "",
        sql,
        flags=re.S,
    )


def test_contract_migration_upgrade_downgrade() -> None:
    """空库/旧数据/回滚/再升级四段式，且老合同不会被伪造归属。"""
    sql_0014 = _offline_sql("upgrade", "20261002_0014")
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_sqlite_sql(sql_0014))
        connection.execute(
            """
            INSERT INTO contracts (
                id, filename, file_size, file_hash, storage_path, status,
                contract_type, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000123",
                "legacy.pdf",
                100,
                "legacy-hash",
                "contracts/legacy.pdf",
                "PARSING",
                "PURCHASE",
                1,
                "2026-10-01T00:00:00+00:00",
                "2026-10-01T00:00:00+00:00",
            ),
        )

        connection.executescript(_sqlite_sql(_offline_sql("upgrade", "20261002_0014:head")))
        columns = {row[1] for row in connection.execute("PRAGMA table_info(contracts)")}
        assert {"created_by_id", "legacy"} <= columns
        row = connection.execute(
            "SELECT created_by_id, legacy FROM contracts WHERE id = ?",
            ("00000000-0000-0000-0000-000000000123",),
        ).fetchone()
        assert row == (None, 1)

        connection.executescript(_sqlite_sql(_offline_sql("downgrade", "head:20261002_0014")))
        columns = {row[1] for row in connection.execute("PRAGMA table_info(contracts)")}
        assert "created_by_id" not in columns
        assert "legacy" not in columns

        connection.executescript(_sqlite_sql(_offline_sql("upgrade", "20261002_0014:head")))
        row = connection.execute(
            "SELECT created_by_id, legacy FROM contracts WHERE id = ?",
            ("00000000-0000-0000-0000-000000000123",),
        ).fetchone()
        assert row == (None, 1)
    finally:
        connection.close()


def test_legacy_contract_marked_not_owned() -> None:
    """历史合同升级后 created_by_id 保持 NULL，legacy 必须为 TRUE。"""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            _sqlite_sql(_offline_sql("upgrade", "20261002_0014"))
        )
        connection.execute(
            """
            INSERT INTO contracts (
                id, filename, file_size, file_hash, storage_path, status,
                contract_type, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000456",
                "legacy-only.pdf",
                1,
                "legacy-only-hash",
                "contracts/legacy-only.pdf",
                "PARSING",
                "PURCHASE",
                1,
                "2026-10-01T00:00:00+00:00",
                "2026-10-01T00:00:00+00:00",
            ),
        )
        connection.executescript(_sqlite_sql(_offline_sql("upgrade", "20261002_0014:head")))
        assert connection.execute(
            "SELECT created_by_id, legacy FROM contracts WHERE id = ?",
            ("00000000-0000-0000-0000-000000000456",),
        ).fetchone() == (None, 1)
    finally:
        connection.close()


class _FakeStorage:
    def stat_object(self, file_key: str) -> StoredObjectInfo:
        del file_key
        return StoredObjectInfo(size=128, etag="owner-etag")

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        del file_key, expires_s
        raise NotImplementedError


class _FakeDispatcher:
    def send(self, task_name: str, *, args: tuple[object, ...] = ()) -> str:
        del task_name, args
        return "celery-owner"


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


def _user_record(user: User) -> UserRecord:
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


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_owner_scope_policy() -> None:
    viewer = _user_record(_user(Role.viewer, "viewer"))
    admin = _user_record(_user(Role.admin, "admin"))
    assert owner_scope(None) == (None, True)
    assert owner_scope(viewer) == (viewer.id, False)
    assert owner_scope(admin) == (None, True)


def test_contract_created_by_owner() -> None:
    async def scenario() -> None:
        engine, factory = await _session_factory()
        try:
            async with factory() as session:
                user = _user(Role.viewer, "owner")
                session.add(user)
                await session.flush()
                context = ServiceContext(
                    session=session,
                    object_storage=_FakeStorage(),
                    task_dispatcher=_FakeDispatcher(),  # type: ignore[arg-type]
                    progress_broker=object(),  # type: ignore[arg-type]
                )
                progress = await ContractService(context).upload(
                    UploadBody(
                        fileKey=generate_file_key("owner.pdf"),
                        fileName="owner.pdf",
                        fileType=FileType.pdf_native,
                    ),
                    _user_record(user),
                )
                contract_id = UUID(str(progress.result["contractId"]))
                contract = await ContractRepository(session).get(contract_id)
                assert contract is not None
                assert str(contract.created_by_id) == str(user.id)
                assert contract.legacy is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_cross_user_contract_forbidden_404_and_admin_exception() -> None:
    async def scenario() -> None:
        engine, factory = await _session_factory()
        try:
            async with factory() as session:
                owner_a = _user(Role.viewer, "owner-a")
                owner_b = _user(Role.viewer, "owner-b")
                admin = _user(Role.admin, "admin")
                session.add_all([owner_a, owner_b, admin])
                await session.flush()
                repo = ContractRepository(session)
                contract_b = await repo.create(
                    ContractCreate(
                        filename="b.pdf",
                        file_size=1,
                        file_hash="owner-b-hash",
                        storage_path="contracts/b.pdf",
                        created_by_id=owner_b.id,
                    )
                )
                task_b = await TaskRepository(session).create(
                    contract_b.id, "PARSE_DOC", "celery-owner-b"
                )
                await session.commit()
                context = ServiceContext(
                    session=session,
                    object_storage=_FakeStorage(),  # type: ignore[arg-type]
                    task_dispatcher=_FakeDispatcher(),  # type: ignore[arg-type]
                    progress_broker=object(),  # type: ignore[arg-type]
                )

                assert await ContractService(context).get(
                    contract_b.id, _user_record(owner_a)
                ) is None
                with pytest.raises(TaskNotFoundError):
                    await TaskService(context).assert_access(
                        task_b.id, _user_record(owner_a)
                    )
                with pytest.raises(ContractNotFoundError):
                    await PreviewService(context).get(
                        contract_b.id, _user_record(owner_a)
                    )

                assert await repo.get(
                    contract_b.id, owner_id=None, include_all=True
                ) is not None
                assert await TaskService(context).assert_access(
                    task_b.id, _user_record(admin)
                ) == task_b
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_generated_artifacts_include_audit_actions() -> None:
    types_text = (
        REPO_ROOT / "apps" / "web-ui" / "src" / "api" / "generated" / "types.ts"
    ).read_text(encoding="utf-8")
    schemas_text = (
        REPO_ROOT / "apps" / "api-gateway" / "app" / "generated" / "schemas.py"
    ).read_text(encoding="utf-8")
    assert "TASK_RECOVERED" in types_text and "FILE_REJECTED" in types_text
    assert "TASK_RECOVERED" in schemas_text and "FILE_REJECTED" in schemas_text
