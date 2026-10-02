"""C74 e2e_smoke 的 T-D7 覆盖守卫（离线；真实链路归 C75/CI）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "scripts" / "e2e_smoke.py"


def test_e2e_smoke_td7_paths() -> None:
    """冒烟脚本必须覆盖认证 / 幂等 / 预览 / 任务恢复四条 T-D7 路径。"""
    source = SMOKE.read_text(encoding="utf-8")
    for marker in (
        "_bootstrap_auth",
        "/auth/login",
        "无凭据访问受保护端点 401",
        "_check_idempotent_replay",
        "同 key 不同 body → 409",
        "_check_preview",
        "/preview",
        "_check_task_recovery",
        "reconcile_tasks.py",
        "--auth-username",
        "--preview-timeout",
    ):
        assert marker in source, f"e2e_smoke 缺少 T-D7 覆盖标记：{marker}"

    completed = subprocess.run(
        [sys.executable, str(SMOKE), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stderr
    assert "--auth-username" in completed.stdout
    assert "--preview-timeout" in completed.stdout
