#!/usr/bin/env python
"""C63：stale 任务回收 CLI（可被 cron / Celery Beat 定时调用）。

用法（仓库根目录）：

    python scripts/reconcile_tasks.py --stale-after 300 --lock-timeout 120

行为：扫描 `audit_tasks` 中「RUNNING 但心跳/更新已过期」的任务，可重放的重试派发，
超限或不可重放的标 `FAILED` + `errorCode`，并逐条写审计。

退出码：0 = 本轮没有终态失败；1 = 有任务被判 FAILED（便于 cron 告警）；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 让脚本直接从仓库根目录运行：把 workspace 内的包加入 sys.path（无需 pip install）
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (
    _REPO_ROOT / ".venv" / "Lib" / "site-packages",
    _REPO_ROOT / "packages" / "rag-contracts",
    _REPO_ROOT / "packages" / "cg-observability",
    _REPO_ROOT / "packages" / "rag-core",
    _REPO_ROOT / "packages" / "ai-engine",
    _REPO_ROOT / "packages" / "doc-processor",
    _REPO_ROOT / "apps" / "api-gateway",
    _REPO_ROOT / "apps" / "async-worker",
):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.db.session import get_session  # noqa: E402
from app.services.context import ServiceContext  # noqa: E402
from app.services.dispatch import CeleryTaskDispatcher  # noqa: E402
from app.services.progress import RedisProgressBroker  # noqa: E402
from app.services.storage import MinioObjectStorage  # noqa: E402
from app.services.task_reconciler import (  # noqa: E402
    DEFAULT_LOCK_TIMEOUT_S,
    DEFAULT_STALE_AFTER_S,
    TaskReconciler,
)


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器（可用 `--stale-after/--lock-timeout` 覆盖阈值）。"""
    parser = argparse.ArgumentParser(description="C63 stale 任务回收与心跳对账")
    parser.add_argument(
        "--stale-after",
        type=int,
        default=DEFAULT_STALE_AFTER_S,
        help=f"失联阈值（秒），默认 {DEFAULT_STALE_AFTER_S}",
    )
    parser.add_argument(
        "--lock-timeout",
        type=int,
        default=DEFAULT_LOCK_TIMEOUT_S,
        help=f"行锁超时（秒），默认 {DEFAULT_LOCK_TIMEOUT_S}",
    )
    return parser


async def run_once(stale_after_s: int, lock_timeout_s: int) -> int:
    """执行一轮 sweep 并打印报告；返回进程退出码。"""
    async for session in get_session():
        context = ServiceContext(
            session=session,
            object_storage=MinioObjectStorage(),
            task_dispatcher=CeleryTaskDispatcher(),
            progress_broker=RedisProgressBroker(),
        )
        reconciler = TaskReconciler(
            context,
            stale_after_s=stale_after_s,
            lock_timeout_s=lock_timeout_s,
        )
        report = await reconciler.sweep()
        print(
            json.dumps(
                {
                    "traceId": report.trace_id,
                    "scanned": report.scanned,
                    "retried": report.retried,
                    "failed": report.failed,
                    "ignored": report.ignored,
                    "details": list(report.details),
                },
                ensure_ascii=False,
            )
        )
        return 1 if report.failed else 0
    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    if args.stale_after <= 0 or args.lock_timeout <= 0:
        print("--stale-after/--lock-timeout 必须为正整数", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_once(args.stale_after, args.lock_timeout))
    except Exception as exc:  # noqa: BLE001 - CLI 兜底：打印安全摘要并给非零退出码
        print(f"reconcile failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
