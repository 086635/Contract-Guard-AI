"""C63 stale 任务回收与心跳对账测试。

离线部分用**内存替身**（任务仓储 / 心跳源 / 派发器 / 审计）覆盖四条验收断言；
真实 PostgreSQL（`list_stale` SQL + 原子 `claim`）在设置 `CG_POSTGRES_DSN` 时执行。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository.contract import (
    ContractCreate,
    ContractDetail,
    ContractRepository,
    TaskRecord,
    TaskRepository,
)
from app.generated.schemas import ContractStatus, Status
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.task_reconciler import (
    ERROR_STALE,
    ERROR_STALE_EXHAUSTED,
    ERROR_UNSUPPORTED_KIND,
    ReconcileReport,
    TaskReconciler,
)
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
REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class FakeHeartbeats:
    """心跳源替身：只有列在 `alive` 里的任务被认为存活。"""

    def __init__(self, alive: set[str] | None = None) -> None:
        self.alive = set(alive or set())
        self.probed: list[str] = []

    async def heartbeat_seen(self, task_id: str) -> bool:
        self.probed.append(task_id)
        return task_id in self.alive


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], str | None]] = []

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        self.calls.append((str(task_name), args, task_id))
        return f"celery-{len(self.calls)}"

    def get_status(self, task_id: str) -> None:
        del task_id
        return None


class FakeAudit:
    """审计仓替身：只记录事件对象。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class FakeTaskRepository:
    """内存版任务仓储（实现 reconciler 用到的全部方法）。"""

    def __init__(self, rows: list[TaskRecord]) -> None:
        self.rows = {row.id: row for row in rows}
        self.locks: dict[UUID, bool] = {}
        self.heartbeat_calls: list[tuple[UUID, datetime | None]] = []

    async def list_stale(self, **_: object) -> list[TaskRecord]:
        return list(self.rows.values())

    async def heartbeat(self, task_id: UUID, *, at: datetime | None = None) -> None:
        self.heartbeat_calls.append((task_id, at))
        self.rows[task_id] = self.rows[task_id].model_copy(
            update={"last_heartbeat_at": at, "updated_at": at}
        )

    async def claim(
        self,
        task_id: UUID,
        *,
        now: datetime,
        lock_expired_before: datetime,
    ) -> bool:
        del now, lock_expired_before
        if self.locks.get(task_id):
            return False
        self.locks[task_id] = True
        return True

    async def record_attempt(
        self,
        task_id: UUID,
        *,
        error_code: str,
        next_retry_at: datetime,
        at: datetime | None = None,
    ) -> TaskRecord:
        current = self.rows[task_id]
        updated = current.model_copy(
            update={
                "attempt_count": current.attempt_count + 1,
                "error_code": error_code,
                "next_retry_at": next_retry_at,
                "last_heartbeat_at": at,
                "locked_at": None,
                "updated_at": at,
            }
        )
        self.rows[task_id] = updated
        return updated

    async def attach_celery_task_id(
        self, task_id: UUID, celery_task_id: str
    ) -> TaskRecord:
        updated = self.rows[task_id].model_copy(
            update={"celery_task_id": celery_task_id}
        )
        self.rows[task_id] = updated
        return updated

    async def mark_failed(
        self,
        task_id: UUID,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> TaskRecord:
        updated = self.rows[task_id].model_copy(
            update={
                "status": Status.failed,
                "error_code": error_code,
                "error_message": error_message,
                "completed_at": NOW,
            }
        )
        self.rows[task_id] = updated
        return updated


class FakeContractRepository:
    def __init__(self, contract: ContractDetail | None) -> None:
        self._contract = contract

    async def get(self, contract_id: UUID) -> ContractDetail | None:
        if self._contract is not None and self._contract.id == contract_id:
            return self._contract
        return None


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _contract(filename: str = "采购合同.pdf") -> ContractDetail:
    now = datetime.now(UTC)
    return ContractDetail(
        id=uuid4(),
        filename=filename,
        file_size=2048,
        file_hash=f"hash-{uuid4().hex[:8]}",
        storage_path=f"contracts/2026/10/{uuid4().hex}-{filename}",
        status=ContractStatus.parsing,
        contract_type=ContractType.OTHER,
        version=1,
        created_at=now,
        updated_at=now,
    )


def _task(
    *,
    contract_id: UUID,
    task_type: str = TaskKind.PARSE_DOC.name,
    status: Status = Status.running,
    attempt_count: int = 0,
    max_attempts: int = 3,
    celery_task_id: str | None = "celery-old",
) -> TaskRecord:
    return TaskRecord.model_validate(
        {
            "id": uuid4(),
            "contract_id": contract_id,
            "task_type": task_type,
            "stage": "queued",
            "progress": 0.1,
            "status": status,
            "celery_task_id": celery_task_id,
            "error_message": None,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "last_heartbeat_at": NOW - timedelta(minutes=30),
            "updated_at": NOW - timedelta(minutes=30),
        }
    )


def _reconciler(
    rows: list[TaskRecord],
    *,
    contract: ContractDetail | None = None,
    heartbeats: FakeHeartbeats | None = None,
    dispatcher: FakeDispatcher | None = None,
    audit: FakeAudit | None = None,
) -> tuple[TaskReconciler, FakeTaskRepository, FakeDispatcher, FakeAudit]:
    tasks = FakeTaskRepository(rows)
    dispatcher = dispatcher or FakeDispatcher()
    audit = audit or FakeAudit()
    context = ServiceContext(
        session=cast(AsyncSession, FakeSession()),
        object_storage=cast(Any, object()),
        task_dispatcher=cast(Any, dispatcher),
        progress_broker=cast(Any, heartbeats or FakeHeartbeats()),
    )
    reconciler = TaskReconciler(
        context,
        stale_after_s=300,
        lock_timeout_s=120,
        heartbeats=heartbeats or FakeHeartbeats(),
        tasks=cast(Any, tasks),
        contracts=cast(Any, FakeContractRepository(contract)),
        audit=cast(Any, audit),
    )
    return reconciler, tasks, dispatcher, audit


# --------------------------------------------------------------------------
# 断言 1：心跳过期可重试
# --------------------------------------------------------------------------
def test_stale_task_reconciled() -> None:
    """心跳过期 → 抢锁重试：attempt+1、写 nextRetryAt、回填新 celery id、写审计。"""
    contract = _contract()
    record = _task(contract_id=contract.id)
    reconciler, tasks, dispatcher, audit = _reconciler([record], contract=contract)

    report = asyncio.run(reconciler.sweep(NOW))

    assert isinstance(report, ReconcileReport)
    assert (report.scanned, report.retried, report.failed, report.ignored) == (
        1,
        1,
        0,
        0,
    )
    assert report.trace_id and report.details

    assert dispatcher.calls[0][0] == TaskKind.PARSE_DOC.value
    assert dispatcher.calls[0][1] == (contract.storage_path, str(contract.id))
    # 幂等派发键带 attempt 序号（同一 attempt 重复派发不会换 key）
    assert dispatcher.calls[0][2] == f"reconcile-{record.id}-1"

    updated = tasks.rows[record.id]
    assert updated.attempt_count == 1
    assert updated.error_code == ERROR_STALE
    assert updated.next_retry_at is not None and updated.next_retry_at > NOW
    assert updated.celery_task_id == "celery-1"
    assert updated.locked_at is None

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action.value == "TASK_RECOVERED"
    assert event.outcome.value == "SUCCESS"
    assert event.trace_id == report.trace_id
    assert event.resource_type == "TASK" and event.resource_id == str(record.id)


def test_fresh_heartbeat_is_not_stale() -> None:
    """Redis 心跳仍在 → 不判失联，只把心跳对账回 DB。"""
    contract = _contract()
    record = _task(contract_id=contract.id)
    heartbeats = FakeHeartbeats({record.celery_task_id or ""})
    reconciler, tasks, dispatcher, audit = _reconciler(
        [record], contract=contract, heartbeats=heartbeats
    )

    report = asyncio.run(reconciler.sweep(NOW))

    assert (report.retried, report.failed, report.ignored) == (0, 0, 1)
    assert dispatcher.calls == []
    assert tasks.heartbeat_calls == [(record.id, NOW)]
    assert audit.events == []


# --------------------------------------------------------------------------
# 断言 2：超限标 FAILED
# --------------------------------------------------------------------------
def test_retry_exhausted_failed() -> None:
    """已用满 max_attempts → 标 FAILED + errorCode（不再重试）。"""
    contract = _contract()
    record = _task(contract_id=contract.id, attempt_count=3, max_attempts=3)
    reconciler, tasks, dispatcher, audit = _reconciler([record], contract=contract)

    report = asyncio.run(reconciler.sweep(NOW))

    assert (report.retried, report.failed) == (0, 1)
    assert dispatcher.calls == []
    updated = tasks.rows[record.id]
    assert updated.status is Status.failed
    assert updated.error_code == ERROR_STALE_EXHAUSTED
    assert updated.completed_at is not None
    assert audit.events[0].outcome.value == "FAILURE"


def test_unsupported_kind_fails_without_guessing() -> None:
    """入参是业务载荷的任务（RUN_AUDIT）不可重放 → 终态失败，绝不猜参数派发。"""
    contract = _contract()
    record = _task(contract_id=contract.id, task_type=TaskKind.RUN_AUDIT.name)
    reconciler, tasks, dispatcher, audit = _reconciler([record], contract=contract)

    report = asyncio.run(reconciler.sweep(NOW))

    assert (report.retried, report.failed) == (0, 1)
    assert dispatcher.calls == []
    assert tasks.rows[record.id].error_code == ERROR_UNSUPPORTED_KIND
    assert audit.events[0].metadata["errorCode"] == ERROR_UNSUPPORTED_KIND


# --------------------------------------------------------------------------
# 断言 3：终态不被改动
# --------------------------------------------------------------------------
def test_terminal_task_ignored() -> None:
    """终态任务即使出现在扫描结果里也必须被忽略（不改状态、不派发）。"""
    contract = _contract()
    done = _task(contract_id=contract.id, status=Status.done)
    failed = _task(contract_id=contract.id, status=Status.failed)
    reconciler, tasks, dispatcher, audit = _reconciler(
        [done, failed], contract=contract
    )

    report = asyncio.run(reconciler.sweep(NOW))

    assert (report.retried, report.failed, report.ignored) == (0, 0, 2)
    assert dispatcher.calls == []
    assert tasks.rows[done.id].status is Status.done
    assert tasks.rows[failed.id].status is Status.failed
    assert audit.events == []


# --------------------------------------------------------------------------
# 断言 4：并发 sweep 不双派
# --------------------------------------------------------------------------
def test_concurrent_sweep_single_dispatch() -> None:
    """拿不到行锁的 sweep 必须跳过：同一任务始终只派发一次。"""
    contract = _contract()
    record = _task(contract_id=contract.id)
    reconciler, tasks, dispatcher, _ = _reconciler([record], contract=contract)

    first = asyncio.run(reconciler.sweep(NOW))
    second = asyncio.run(reconciler.sweep(NOW))

    assert (first.retried, first.failed) == (1, 0)
    assert (second.retried, second.failed) == (0, 0)
    assert second.ignored == 1
    assert len(dispatcher.calls) == 1
    assert tasks.rows[record.id].attempt_count == 1


def test_sweep_rejects_naive_now() -> None:
    """naive datetime 直接拒绝（阈值必须基于明确时区）。"""
    contract = _contract()
    reconciler, _, _, _ = _reconciler(
        [_task(contract_id=contract.id)], contract=contract
    )

    with pytest.raises(ValueError, match="时区"):
        asyncio.run(reconciler.sweep(datetime(2026, 10, 1, 12, 0)))


def test_scan_upload_and_convert_replay_args() -> None:
    """扫描/转换任务按各自跨进程签名重建参数（不新写第二套映射）。"""
    contract = _contract("采购合同.pdf")
    scan = _task(contract_id=contract.id, task_type=TaskKind.SCAN_UPLOAD.name)
    convert = _task(contract_id=contract.id, task_type=TaskKind.CONVERT_DOC.name)
    reconciler, _, dispatcher, _ = _reconciler([scan, convert], contract=contract)

    asyncio.run(reconciler.sweep(NOW))

    by_kind = {name: args for name, args, _ in dispatcher.calls}
    assert by_kind["scan_upload"] == (
        contract.storage_path,
        contract.filename,
        "PDF_NATIVE",
        str(contract.id),
    )
    assert by_kind["convert_doc"] == (str(contract.id), contract.storage_path)


# --------------------------------------------------------------------------
# 可定时执行的 CLI 入口
# --------------------------------------------------------------------------
def test_reconcile_cli_entrypoint() -> None:
    """`scripts/reconcile_tasks.py` 可执行：--help 为 0，非法阈值为 2。"""
    script = REPO_ROOT / "scripts" / "reconcile_tasks.py"
    env = os.environ.copy()
    env.setdefault("CG_TASK_STALE_AFTER_S", "300")
    help_run = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert help_run.returncode == 0, help_run.stderr
    assert "--stale-after" in help_run.stdout

    bad_run = subprocess.run(
        [sys.executable, str(script), "--stale-after", "0"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert bad_run.returncode == 2


# --------------------------------------------------------------------------
# 真实 PostgreSQL：SQL 过滤与原子抢锁
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C63 reconcile PostgreSQL 集成测试",
)
def test_reconciler_postgres_roundtrip() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_postgres(POSTGRES_DSN))


async def _exercise_postgres(dsn: str) -> None:
    schema = new_schema_name("cg_c63")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract = await ContractRepository(session).create(
                ContractCreate(
                    filename="stale.pdf",
                    file_size=1024,
                    file_hash="hash-c63",
                    storage_path="contracts/2026/10/c63.pdf",
                    status=ContractStatus.parsing,
                    contract_type=ContractType.OTHER,
                )
            )
            tasks = TaskRepository(session)
            stale = await tasks.create(
                contract.id, TaskKind.PARSE_DOC.name, "celery-c63"
            )
            await tasks.heartbeat(stale.id, at=NOW - timedelta(minutes=30))
            terminal = await tasks.create(
                contract.id, TaskKind.PARSE_DOC.name, "celery-c63b"
            )
            await tasks.mark_failed(terminal.id, error_code="X")
            await session.commit()

            stale_before = NOW - timedelta(seconds=300)
            lock_expired_before = NOW - timedelta(seconds=120)
            found = await tasks.list_stale(
                stale_before=stale_before,
                lock_expired_before=lock_expired_before,
            )
            assert {row.id for row in found} == {stale.id}  # 终态被 SQL 过滤

            assert await tasks.claim(
                stale.id, now=NOW, lock_expired_before=lock_expired_before
            )
            # 第二次抢锁必须失败（并发 sweep 不双派）
            assert not await tasks.claim(
                stale.id, now=NOW, lock_expired_before=lock_expired_before
            )
            await session.rollback()
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
