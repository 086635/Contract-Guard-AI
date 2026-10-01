"""C62 任务可靠性模型测试（离线为主）。

离线覆盖：可靠性字段、有限重试策略（退避/上限）、外部 Status 兼容、迁移往返 SQL。
真实 PostgreSQL 往返（含 `attach_celery_task_id` / `mark_failed`）在设置
`CG_POSTGRES_DSN` 时执行，否则按统一约定 skip。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config

from app.db.models import _AUDIT_TASK_TYPES, DEFAULT_MAX_ATTEMPTS, AuditTask
from app.db.repository.contract import (
    RETRY_BACKOFF_BASE_S,
    RETRY_BACKOFF_CAP_S,
    ContractCreate,
    ContractRepository,
    TaskRecord,
    TaskRepository,
)
from app.generated.schemas import ContractStatus, Status
from rag_contracts.enums import ContractType
from tests.service_testkit import (
    create_schema,
    drop_schema,
    migrate,
    new_schema_name,
    schema_engine,
    session_factory,
)

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
API_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = API_GATEWAY_ROOT / "alembic.ini"
OFFLINE_DSN = "postgresql+asyncpg://cg:cg@localhost:5432/cg"
OPENAPI_PATH = API_GATEWAY_ROOT.parents[1] / "packages" / "schema" / "openapi.yaml"

RELIABILITY_FIELDS = (
    "attempt_count",
    "max_attempts",
    "last_heartbeat_at",
    "next_retry_at",
    "error_code",
    "locked_at",
    "completed_at",
)
NULLABLE_FIELDS = (
    "last_heartbeat_at",
    "next_retry_at",
    "error_code",
    "locked_at",
    "completed_at",
)


def _record(**overrides: object) -> TaskRecord:
    payload: dict[str, object] = {
        "id": uuid4(),
        "contract_id": uuid4(),
        "task_type": "PARSE_DOC",
        "stage": "queued",
        "progress": 0.0,
        "status": Status.running,
        "celery_task_id": "celery-1",
        "error_message": None,
    }
    payload.update(overrides)
    return TaskRecord.model_validate(payload)


# --------------------------------------------------------------------------
# 断言 1：旧任务升级后字段可空 + 模型字段齐备
# --------------------------------------------------------------------------
def test_task_reliability_fields() -> None:
    """7 个可靠性字段齐备；计数字段非空默认、其余可空（兼容旧任务）。"""
    columns = AuditTask.__table__.c
    for field in RELIABILITY_FIELDS:
        assert field in columns, field

    for field in ("attempt_count", "max_attempts"):
        assert columns[field].nullable is False, field
        assert columns[field].default is not None, field

    for field in NULLABLE_FIELDS:
        assert columns[field].nullable is True, field

    assert int(columns["max_attempts"].default.arg) == DEFAULT_MAX_ATTEMPTS
    # 默认值：新任务 0 次尝试、上限 DEFAULT_MAX_ATTEMPTS
    fresh = _record()
    assert fresh.attempt_count == 0
    assert fresh.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert fresh.error_code is None and fresh.completed_at is None


def test_task_type_whitelist_covers_dispatched_tasks() -> None:
    """C57 扫描 / C59 转换必须已登记进任务类型白名单（否则写库被 CHECK 拒绝）。"""
    assert {"SCAN_UPLOAD", "CONVERT_DOC"} <= set(_AUDIT_TASK_TYPES)
    # 白名单即 SQLAlchemy 枚举的取值来源
    enum_values = AuditTask.__table__.c.task_type.type.enums
    assert set(enum_values) == set(_AUDIT_TASK_TYPES)


# --------------------------------------------------------------------------
# 断言 3：重试次数不超过 max_attempts（有限 + 指数退避）
# --------------------------------------------------------------------------
def test_retry_cap() -> None:
    """退避按指数增长且封顶；达到 max_attempts 后一律不再重试。"""
    delays = [_record(attempt_count=count).retry_delay_seconds for count in (1, 2, 3)]
    assert delays == [
        RETRY_BACKOFF_BASE_S,
        RETRY_BACKOFF_BASE_S * 2,
        RETRY_BACKOFF_BASE_S * 4,
    ]
    wide = [_record(attempt_count=count).retry_delay_seconds for count in range(1, 20)]
    assert all(delay <= RETRY_BACKOFF_CAP_S for delay in wide)
    assert wide[-1] == RETRY_BACKOFF_CAP_S  # 长退避封顶，不会无限增长

    at_cap = _record(attempt_count=3, max_attempts=3)
    assert at_cap.can_retry is False
    assert _record(attempt_count=2, max_attempts=3).can_retry is True
    # 上限至少为 1（max_attempts=0 退化为「只允许 1 次尝试」）
    assert _record(attempt_count=0, max_attempts=0).can_retry is True
    assert _record(attempt_count=1, max_attempts=0).can_retry is False
    assert _record(attempt_count=0, max_attempts=1).can_retry is True
    assert _record(attempt_count=1, max_attempts=1).can_retry is False


def test_retry_then_success() -> None:
    """第一次失败 → 允许重试并给出退避；重试成功后不再重试。"""
    first = _record(attempt_count=1)
    assert first.can_retry is True
    assert first.retry_delay_seconds == RETRY_BACKOFF_BASE_S

    succeeded = first.model_copy(update={"status": Status.done, "attempt_count": 2})
    assert succeeded.can_retry is False


def test_retry_exhausted_failed() -> None:
    """连续失败到 max_attempts → 不再重试（进入终态由 reconciler 标记 FAILED）。"""
    # 已有 1 次尝试失败；max_attempts=3 ⇒ 最多再重试 2 次
    record = _record(attempt_count=1, max_attempts=3, status=Status.failed)
    retries = 0
    retry_delays: list[float] = []
    while record.can_retry:
        retry_delays.append(record.retry_delay_seconds)
        retries += 1
        record = record.model_copy(update={"attempt_count": 1 + retries})

    assert retries == 2  # 1 + 2 = 3 次尝试，恰好用满 max_attempts，不会无限重试
    assert retry_delays == [5.0, 10.0]
    assert record.attempt_count == record.max_attempts
    assert record.can_retry is False


# --------------------------------------------------------------------------
# 断言 4：外部 Status 不变
# --------------------------------------------------------------------------
def test_external_status_enum_unchanged() -> None:
    """外部 `Status` 仍只有 RUNNING/DONE/FAILED（不新增 QUEUED/CANCELLED）。"""
    assert [status.value for status in Status] == ["RUNNING", "DONE", "FAILED"]

    openapi_text = OPENAPI_PATH.read_text(encoding="utf-8")
    # 任务状态在 TaskProgress / ReportData 里是内联枚举，取值必须仍是三态
    inline_task_status = [
        line
        for line in openapi_text.splitlines()
        if "enum: [RUNNING, DONE, FAILED]" in line
    ]
    assert len(inline_task_status) >= 2
    assert "QUEUED" not in openapi_text and "CANCELLED" not in openapi_text
    # 预览状态是**独立**枚举（PENDING 只属于预览，不属于任务 Status）
    assert "enum: [PENDING, RUNNING, DONE, FAILED]" in openapi_text


# --------------------------------------------------------------------------
# 迁移往返（离线 SQL + SQLite 执行）
# --------------------------------------------------------------------------
def _alembic_config(dsn: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", dsn)
    return config


def _offline_sql(action: str, revision: str) -> str:
    config = _alembic_config(OFFLINE_DSN)
    buffer = StringIO()
    with redirect_stdout(buffer):
        if action == "upgrade":
            command.upgrade(config, revision, sql=True)
        else:
            command.downgrade(config, revision, sql=True)
    return buffer.getvalue()


def _audit_task_columns(connection: sqlite3.Connection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info(audit_tasks)")}


def _audit_task_ddl(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_tasks'"
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_task_migration_roundtrip() -> None:
    """upgrade 加齐字段并放宽类型白名单；downgrade 可完整回退。"""
    upgrade_sql = _offline_sql("upgrade", "head")
    downgrade_sql = _offline_sql("downgrade", "head:20261001_0011")
    assert "audit_tasks_old" in upgrade_sql  # 便携式重建（rename/create/insert/drop）

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(upgrade_sql)
        columns = _audit_task_columns(connection)
        assert set(RELIABILITY_FIELDS) <= columns

        ddl = _audit_task_ddl(connection)
        for task_type in ("REWRITE", "SCAN_UPLOAD", "CONVERT_DOC"):
            assert task_type in ddl, task_type
        assert "ck_audit_tasks_progress" in ddl

        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(audit_tasks)")
        }
        assert {"ix_audit_tasks_contract_type", "ix_audit_tasks_status"} <= indexes

        connection.executescript(downgrade_sql)
        after = _audit_task_columns(connection)
        assert after.isdisjoint(RELIABILITY_FIELDS)
        rolled_back_ddl = _audit_task_ddl(connection)
        assert "SCAN_UPLOAD" not in rolled_back_ddl
        assert "CONVERT_DOC" not in rolled_back_ddl
    finally:
        connection.close()


# --------------------------------------------------------------------------
# 真实 PostgreSQL：仓储字段往返（派发前建行 → 回填 → 终态）
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C62 任务可靠性 PostgreSQL 集成测试",
)
def test_task_repository_reliability_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_task_repository(POSTGRES_DSN))


async def _exercise_task_repository(dsn: str) -> None:
    schema = new_schema_name("cg_c62")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract = await ContractRepository(session).create(
                ContractCreate(
                    filename="任务可靠性合同.pdf",
                    file_size=1024,
                    file_hash="hash-c62",
                    storage_path="contracts/2026/10/c62.pdf",
                    status=ContractStatus.parsing,
                    contract_type=ContractType.OTHER,
                )
            )
            tasks = TaskRepository(session)

            # 派发前先建行（celery id 未知）
            created = await tasks.create(
                contract.id, "SCAN_UPLOAD", None, max_attempts=2
            )
            assert created.celery_task_id is None
            assert created.attempt_count == 0 and created.max_attempts == 2

            attached = await tasks.attach_celery_task_id(created.id, "celery-c62-1")
            assert attached.celery_task_id == "celery-c62-1"

            failed = await tasks.mark_failed(
                created.id,
                error_code="DISPATCH_FAILED",
                error_message="ConnectionError",
            )
            assert failed.status is Status.failed
            assert failed.error_code == "DISPATCH_FAILED"
            assert failed.completed_at is not None
            assert failed.can_retry is True  # attempt_count 仍是 0（未重试过）

            # CONVERT_DOC 必须能被写入（C59 的任务行登记依赖放宽后的 CHECK）
            convert = await tasks.create(contract.id, "CONVERT_DOC", "celery-c62-2")
            assert convert.task_type == "CONVERT_DOC"

            await session.commit()

            reread = await tasks.get(created.id)
            assert reread is not None and reread.error_code == "DISPATCH_FAILED"
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
