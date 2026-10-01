"""C63 任务 stale 回收与心跳对账。

设计要点：
1. **DB 是状态源**：扫描 `audit_tasks` 的 RUNNING 行；Redis 心跳只作存活证据（只读）。
2. **先锁行再派发**：`TaskRepository.claim()` 是原子条件更新，抢不到就跳过 →
   并发 sweep 不会重复派发（§⑥-2 / 断言 4）。
3. **有限重试**：复用 C62 的 `TaskRecord.can_retry` / `retry_delay_seconds` / `max_attempts`，
   超限或不可重放一律收敛为终态 FAILED 并写 `errorCode`（§⑥-3）。
4. **不伪造输入**：可安全重放的派发参数按任务类型重建；入参是**业务载荷**的任务
   （`RUN_AUDIT`/`GEN_REPORT`/`REWRITE`）行里没有快照，不猜参数，直接终态失败 + 审计，
   交人工重触发。
5. 每次收敛都写审计（`AuditAction.task_recovered`）并带 `traceId`（②「记录 reconcile 审计与 traceId」）。

对应：`文件集合/codex/4.C63Stale任务回收与心跳对账.md`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db.models import AuditAction, AuditOutcome
from app.db.repository.audit import AuditEventCreate, AuditRepository
from app.db.repository.contract import (
    ContractDetail,
    ContractRepository,
    TaskRecord,
    TaskRepository,
)
from app.generated.schemas import Status
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.projector import ContractProjector
from cg_observability import get_logger, log_event, new_trace_id

_LOG = get_logger("api_gateway.services.task_reconciler")

#: 失联阈值（秒）：超过这个时间既无心跳也无更新，即判定 stale（可用环境变量覆盖）。
DEFAULT_STALE_AFTER_S = 300
#: 行锁超时（秒）：锁过期后允许其它 sweep 接管，避免一次崩溃把任务永久锁死。
DEFAULT_LOCK_TIMEOUT_S = 120

#: 可安全重放的任务类型（入参可从合同/预览行重建）；其余类型不猜参数。
REPLAYABLE_TASK_TYPES = (
    TaskKind.PARSE_DOC.name,
    TaskKind.SCAN_UPLOAD.name,
    TaskKind.CONVERT_DOC.name,
)

#: 收敛错误码（唯一处）。
ERROR_STALE = "STALE_TASK"
ERROR_STALE_EXHAUSTED = "STALE_TASK_EXHAUSTED"
ERROR_UNSUPPORTED_KIND = "RECONCILE_UNSUPPORTED_KIND"


def task_kind_of(task_type: str) -> TaskKind | None:
    """把**库里的任务类型名**（`TaskKind.name`，如 `PARSE_DOC`）反查回派发用枚举。

    注意：库中存的是枚举**名**（大写），枚举值是派发用的任务名（小写）——
    二者不可混用（`TaskKind("PARSE_DOC")` 会抛 ValueError）。
    """
    try:
        return TaskKind[task_type]
    except KeyError:
        return None


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """一次 sweep 的结果（C63 §⑤：retried / failed / ignored）。"""

    scanned: int = 0
    retried: int = 0
    failed: int = 0
    ignored: int = 0
    trace_id: str = ""
    details: tuple[str, ...] = ()

    @property
    def changed(self) -> int:
        """本次真正变更状态的任务数。"""
        return self.retried + self.failed


class TaskReconciler:
    """扫描失联任务并收敛（重试或终态失败）。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        stale_after_s: int | None = None,
        lock_timeout_s: int | None = None,
        heartbeats: object | None = None,
        tasks: TaskRepository | None = None,
        contracts: ContractRepository | None = None,
        audit: AuditRepository | None = None,
    ) -> None:
        """构造 reonciler；仓储/心跳源可注入替身（离线可测）。"""
        self._context = context
        self._tasks = tasks or TaskRepository(context.session)
        self._contracts = contracts or ContractRepository(context.session)
        self._audit = audit or AuditRepository(context.session)
        self._heartbeats = heartbeats or context.progress_broker
        self._stale_after_s = int(
            stale_after_s
            if stale_after_s is not None
            else os.getenv("CG_TASK_STALE_AFTER_S", DEFAULT_STALE_AFTER_S)
        )
        self._lock_timeout_s = int(
            lock_timeout_s
            if lock_timeout_s is not None
            else os.getenv("CG_TASK_LOCK_TIMEOUT_S", DEFAULT_LOCK_TIMEOUT_S)
        )

    @property
    def stale_after_s(self) -> int:
        """当前失联阈值（秒）。"""
        return self._stale_after_s

    async def sweep(self, now: datetime | None = None) -> ReconcileReport:
        """扫描一轮失联任务并收敛。

        Args:
            now: 判定基准时刻（**必须带时区**；缺省用 UTC 当前时间）。

        Returns:
            `ReconcileReport`（scanned / retried / failed / ignored + traceId）。

        Raises:
            ValueError: `now` 是 naive datetime（§⑥-1：统一 UTC、阈值明确）。
        """
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("sweep(now) 必须带时区")
        moment = moment.astimezone(UTC)
        stale_before = moment - timedelta(seconds=self._stale_after_s)
        lock_expired_before = moment - timedelta(seconds=self._lock_timeout_s)
        trace_id = new_trace_id()

        candidates = await self._tasks.list_stale(
            stale_before=stale_before,
            lock_expired_before=lock_expired_before,
        )
        retried = failed = ignored = 0
        details: list[str] = []

        for record in candidates:
            if record.status is not Status.running:
                # 终态任务一律不动（断言 3；扫描已过滤，这里是二次防御）
                ignored += 1
                continue

            task_key = record.celery_task_id or str(record.id)
            if await self._heartbeats.heartbeat_seen(task_key):  # type: ignore[attr-defined]
                # 心跳仍在：不是失联，只把心跳对账回 DB（Redis 只做传输）
                await self._tasks.heartbeat(record.id, at=moment)
                ignored += 1
                continue

            # 先锁行再派发：抢不到说明另一个 sweep 正在处理（断言 4）
            if not await self._tasks.claim(
                record.id,
                now=moment,
                lock_expired_before=lock_expired_before,
            ):
                ignored += 1
                continue

            kind = task_kind_of(record.task_type)
            args = await self._replay_args(record, kind)
            if kind is not None and args is not None and record.can_retry:
                next_retry_at = moment + timedelta(seconds=record.retry_delay_seconds)
                updated = await self._tasks.record_attempt(
                    record.id,
                    error_code=ERROR_STALE,
                    next_retry_at=next_retry_at,
                    at=moment,
                )
                celery_task_id = self._context.task_dispatcher.send(
                    kind,
                    args=args,
                    task_id=f"reconcile-{record.id}-{updated.attempt_count}",
                )
                await self._tasks.attach_celery_task_id(record.id, celery_task_id)
                retried += 1
                details.append(f"retried:{record.id}:attempt={updated.attempt_count}")
                await self._write_audit(
                    record,
                    outcome=AuditOutcome.success,
                    trace_id=trace_id,
                    error_code=ERROR_STALE,
                    reconcile="RETRIED",
                )
                continue

            error_code = (
                ERROR_STALE_EXHAUSTED if args is not None else ERROR_UNSUPPORTED_KIND
            )
            await self._tasks.mark_failed(
                record.id,
                error_code=error_code,
                error_message=(
                    "重试次数用尽"
                    if args is not None
                    else "入参含业务载荷，无法安全重放"
                ),
            )
            failed += 1
            details.append(f"failed:{record.id}:{error_code}")
            await self._write_audit(
                record,
                outcome=AuditOutcome.failure,
                trace_id=trace_id,
                error_code=error_code,
                reconcile="FAILED",
            )

        await self._context.session.commit()
        report = ReconcileReport(
            scanned=len(candidates),
            retried=retried,
            failed=failed,
            ignored=ignored,
            trace_id=trace_id,
            details=tuple(details),
        )
        log_event(
            _LOG,
            "task.reconcile",
            level="INFO" if not report.changed else "WARNING",
            trace_id=trace_id,
            scanned=report.scanned,
            retried=report.retried,
            failed=report.failed,
            ignored=report.ignored,
            stale_after_s=self._stale_after_s,
        )
        return report

    async def _replay_args(
        self,
        record: TaskRecord,
        kind: TaskKind | None,
    ) -> tuple[object, ...] | None:
        """按任务类型重建**可安全重放**的派发参数；无法重建返回 None。"""
        if kind is None or kind.name not in REPLAYABLE_TASK_TYPES:
            return None
        contract = await self._contracts.get(record.contract_id)
        if contract is None:
            return None
        return self._args_for(record, contract)

    @staticmethod
    def _args_for(
        record: TaskRecord,
        contract: ContractDetail,
    ) -> tuple[object, ...] | None:
        """重建规则（唯一定处）：与各任务的跨进程签名一一对应。"""
        if record.task_type == TaskKind.PARSE_DOC.name:
            return (contract.storage_path, str(record.contract_id))
        if record.task_type == TaskKind.SCAN_UPLOAD.name:
            # 复用既有「文件名 → FileType」投影，不另写一份扩展名映射
            file_type = ContractProjector._file_type(contract.filename).value
            return (
                contract.storage_path,
                contract.filename,
                file_type,
                str(record.contract_id),
            )
        if record.task_type == TaskKind.CONVERT_DOC.name:
            # C59 接口：convert_doc(contract_id, source_key)，源仍是原始 DOCX 对象
            return (str(record.contract_id), contract.storage_path)
        return None

    async def _write_audit(
        self,
        record: TaskRecord,
        *,
        outcome: AuditOutcome,
        trace_id: str,
        error_code: str,
        reconcile: str,
    ) -> None:
        """写一条 reconcile 审计（只含任务元信息，不含业务正文）。"""
        await self._audit.append(
            AuditEventCreate(
                actor_id=None,
                actor_role=None,
                action=AuditAction.task_recovered,
                resource_type="TASK",
                resource_id=str(record.id),
                outcome=outcome,
                trace_id=trace_id,
                metadata={
                    "contractId": str(record.contract_id),
                    "taskType": record.task_type,
                    "errorCode": error_code,
                    "reconcile": reconcile,
                    "attemptCount": record.attempt_count,
                    "maxAttempts": record.max_attempts,
                },
            )
        )


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_S",
    "DEFAULT_STALE_AFTER_S",
    "ERROR_STALE",
    "ERROR_STALE_EXHAUSTED",
    "ERROR_UNSUPPORTED_KIND",
    "REPLAYABLE_TASK_TYPES",
    "ReconcileReport",
    "TaskReconciler",
    "task_kind_of",
]
