"""C69 备份/恢复脚本契约测试（CLI 参数 / dry-run / 保留策略 / 密钥不外泄）。

这些用例只依赖 bash 与脚本自带的伪工具（pg_dump / pg_restore / psql / mc），
不需要真实 PostgreSQL / MinIO：真实环境下的恢复演练由交付报告登记。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
BACKUP_PG = SCRIPTS / "backup_postgres.sh"
BACKUP_MINIO = SCRIPTS / "backup_minio.sh"
RESTORE_DRILL = SCRIPTS / "restore_drill.sh"

FIXED_TS = "20261001T120000Z"
DSN_URL = "postgresql://cg:supersecret@db:5432/cg"

_BASH_CACHE: str | None = None


def _posix(path: Path) -> str:
    return path.resolve().as_posix()


def _write(path: Path, text: str) -> None:
    """按 LF 写文本，避免 Windows 换行转换影响 shell 侧比对。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _bash() -> str:
    global _BASH_CACHE
    if _BASH_CACHE:
        return _BASH_CACHE

    candidates: list[str] = []
    configured = os.environ.get("CG_TEST_BASH")
    if configured:
        candidates.append(configured)
    candidates += [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        r"D:\git\Git\bin\bash.exe",
    ]
    for name in ("git-bash", "bash"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates += ["/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash"]

    tried: list[str] = []
    for candidate in candidates:
        if candidate in tried:
            continue
        tried.append(candidate)
        if (
            os.path.isabs(candidate)
            and os.name == "nt"
            and not Path(candidate).exists()
        ):
            continue
        if not os.path.isabs(candidate) and shutil.which(candidate) is None:
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "printf cg-bash-ok"],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and b"cg-bash-ok" in probe.stdout:
            _BASH_CACHE = candidate
            return candidate

    pytest.skip(f"未找到可用的 bash 解释器（已尝试：{tried}）")


def _run(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
    expect_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [_bash(), _posix(script), *args],
        cwd=str(REPO_ROOT),
        env=env if env is not None else dict(os.environ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if not expect_failure and proc.returncode != 0:
        raise AssertionError(
            f"{script.name} 退出码 {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


PG_DUMP_STUB = """#!/bin/bash
printf 'pg_dump %s\\n' "$*" >> "${FAKE_LOG:-/dev/null}"
out=""
for a in "$@"; do
  case "$a" in
    --file=*) out="${a#--file=}" ;;
    --dbname=*) printf 'dsn=%s\\n' "${a#--dbname=}" ;;
  esac
done
if [ -n "$out" ]; then printf 'FAKE-PG-DUMP\\n' > "$out"; fi
exit "${FAKE_PG_DUMP_EXIT:-0}"
"""

PG_RESTORE_STUB = """#!/bin/bash
printf 'pg_restore %s\\n' "$*" >> "${FAKE_LOG:-/dev/null}"
exit "${FAKE_PG_RESTORE_EXIT:-0}"
"""

PSQL_STUB = """#!/bin/bash
printf 'psql %s\\n' "$*" >> "${FAKE_LOG:-/dev/null}"
sql=""
for a in "$@"; do
  case "$a" in
    SELECT*) sql="$a" ;;
  esac
done
case "$sql" in
  *version_num*) printf '%s\\n' "${FAKE_ALEMBIC_VERSION:-rev-test-1}" ;;
  *) printf '1\\n' ;;
esac
exit "${FAKE_PSQL_EXIT:-0}"
"""

MC_STUB = """#!/bin/bash
printf 'mc %s\\n' "$*" >> "${FAKE_LOG:-/dev/null}"
cmd="${1:-}"
case "$cmd" in
  mirror)
    if [ "${FAKE_MC_MIRROR_EXIT:-0}" != "0" ]; then exit "$FAKE_MC_MIRROR_EXIT"; fi
    dest=""
    for a in "$@"; do dest="$a"; done
    case "$dest" in
      /*|[A-Za-z]:/*) mkdir -p "$dest" 2>/dev/null || true ;;
      *) dest="" ;;
    esac
    if [ -n "$dest" ] && [ -n "${FAKE_MC_MIRROR_FILES:-}" ] && [ -f "$FAKE_MC_MIRROR_FILES" ]; then
      while IFS= read -r rel; do
        rel="${rel%$'\r'}"
        [ -n "$rel" ] || continue
        mkdir -p "$dest/$(dirname "$rel")" 2>/dev/null || true
        printf 'obj' > "$dest/$rel"
      done <"$FAKE_MC_MIRROR_FILES"
    fi
    exit 0
    ;;
  ls)
    if [ "${FAKE_MC_LS_EXIT:-0}" != "0" ]; then exit "$FAKE_MC_LS_EXIT"; fi
    cat "${FAKE_MC_LS_JSON:-/dev/null}"
    exit 0
    ;;
esac
exit 0
"""


class FakeTools:
    """在临时目录伪造 pg_dump/pg_restore/psql/mc，并记录调用参数。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "fakebin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.log = root / "calls.log"
        self.mirror_files = root / "mirror_files.txt"
        self.ls_json = root / "ls.json"
        _write(self.mirror_files, "a.pdf\nsub/b.docx\n")
        _write(
            self.ls_json,
            '{"status":"success","type":"file","size":10,"key":"a.pdf","etag":"e1"}\n'
            '{"status":"success","type":"file","size":20,"key":"sub/b.docx","etag":"e2"}\n',
        )
        for name, body in (
            ("pg_dump", PG_DUMP_STUB),
            ("pg_restore", PG_RESTORE_STUB),
            ("psql", PSQL_STUB),
            ("mc", MC_STUB),
        ):
            path = self.bin / name
            path.write_bytes(body.encode("utf-8"))
            path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "CG_PG_DUMP_BIN": _posix(self.bin / "pg_dump"),
                "CG_PG_RESTORE_BIN": _posix(self.bin / "pg_restore"),
                "CG_PSQL_BIN": _posix(self.bin / "psql"),
                "CG_MINIO_MC_BIN": _posix(self.bin / "mc"),
                "FAKE_LOG": _posix(self.log),
                "FAKE_MC_MIRROR_FILES": _posix(self.mirror_files),
                "FAKE_MC_LS_JSON": _posix(self.ls_json),
            }
        )
        env.update(extra)
        return env

    def calls(self) -> str:
        if not self.log.exists():
            return ""
        return self.log.read_text(encoding="utf-8")

    def call_names(self) -> list[str]:
        return [
            line.split(" ", 1)[0] for line in self.calls().splitlines() if line.strip()
        ]


@pytest.fixture()
def tools(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path)


def _make_minio_backup(root: Path, count: int = 2) -> Path:
    backup = root / f"minio_local_contracts_{FIXED_TS}"
    objects = backup / "objects"
    (objects / "sub").mkdir(parents=True, exist_ok=True)
    (objects / "a.pdf").write_bytes(b"obj")
    (objects / "sub" / "b.docx").write_bytes(b"obj")
    _write(backup / "objects.tsv", "10\ta.pdf\n20\tsub/b.docx\n")
    _write(backup / "manifest.txt", f"object_count={count}\n")
    return backup


def _make_pg_backup(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    dump = root / f"postgres_cg_{FIXED_TS}.dump"
    dump.write_bytes(b"FAKE-PG-DUMP\n")
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    meta = root / f"postgres_cg_{FIXED_TS}.meta"
    _write(
        meta,
        (
            "component=C69\n"
            f"artifact=postgres_cg_{FIXED_TS}.dump\n"
            f"sha256={digest}\n"
            "alembic_version=rev-1\n"
        ),
    )
    return dump, meta


def _snapshot(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def test_backup_args(tmp_path: Path) -> None:
    for script, needle in (
        (BACKUP_PG, "--dsn"),
        (BACKUP_MINIO, "--alias"),
        (RESTORE_DRILL, "--pg-backup"),
    ):
        proc = _run(script, "--help")
        assert needle in (proc.stdout + proc.stderr)

    proc = _run(BACKUP_PG, "--out", _posix(tmp_path), expect_failure=True)
    assert proc.returncode == 2
    assert "--dsn" in proc.stderr

    proc = _run(
        BACKUP_MINIO,
        "--bucket",
        "contracts",
        "--out",
        _posix(tmp_path),
        expect_failure=True,
    )
    assert proc.returncode == 2
    assert "--alias" in proc.stderr

    proc = _run(RESTORE_DRILL, expect_failure=True)
    assert proc.returncode == 2
    assert "--pg-backup" in proc.stderr

    proc = _run(BACKUP_PG, "--nope", expect_failure=True)
    assert proc.returncode == 2

    proc = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(tmp_path),
        "--retain",
        "not-a-number",
        "--dry-run",
        expect_failure=True,
    )
    assert proc.returncode == 2
    assert "retain" in proc.stderr


def test_backup_postgres_creates_verifiable_artifact(
    tools: FakeTools, tmp_path: Path
) -> None:
    out = tmp_path / "backups"
    proc = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(out),
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "artifact=postgres_cg_" in proc.stdout

    dump = out / f"postgres_cg_{FIXED_TS}.dump"
    assert dump.read_bytes() == b"FAKE-PG-DUMP\n"
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert digest in (out / f"postgres_cg_{FIXED_TS}.sha256").read_text(
        encoding="utf-8"
    )

    meta = (out / f"postgres_cg_{FIXED_TS}.meta").read_text(encoding="utf-8")
    assert "alembic_version=rev-test-1" in meta
    assert f"sha256={digest}" in meta
    assert "exit_code=0" in (out / f"postgres_cg_{FIXED_TS}.log").read_text(
        encoding="utf-8"
    )
    assert "pg_restore --list" in tools.calls()


def test_backup_postgres_failure_reports_exit_code(
    tools: FakeTools, tmp_path: Path
) -> None:
    out = tmp_path / "backups"
    proc = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(out),
        env=tools.env(FAKE_PG_DUMP_EXIT="7", CG_BACKUP_TIMESTAMP=FIXED_TS),
        expect_failure=True,
    )
    assert proc.returncode == 4
    assert "exit_code=4" in proc.stderr
    assert not (out / f"postgres_cg_{FIXED_TS}.dump").exists()
    log = (out / f"postgres_cg_{FIXED_TS}.log").read_text(encoding="utf-8")
    assert "exit_code=7" in log


def test_backup_no_secret_in_output(tools: FakeTools, tmp_path: Path) -> None:
    out = tmp_path / "url-backups"
    proc = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(out),
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "supersecret" not in (proc.stdout + proc.stderr)
    for suffix in ("meta", "log"):
        text = (out / f"postgres_cg_{FIXED_TS}.{suffix}").read_text(encoding="utf-8")
        assert "supersecret" not in text, suffix
    assert "***" in (out / f"postgres_cg_{FIXED_TS}.log").read_text(encoding="utf-8")

    kv_secret = "kv-password-42"
    out2 = tmp_path / "kv-backups"
    proc2 = _run(
        BACKUP_PG,
        "--dsn",
        f"host=db user=cg password={kv_secret} dbname=cg",
        "--out",
        _posix(out2),
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert kv_secret not in (proc2.stdout + proc2.stderr)
    assert kv_secret not in (out2 / f"postgres_cg_{FIXED_TS}.log").read_text(
        encoding="utf-8"
    )


def test_backup_restore_dry_run(tools: FakeTools, tmp_path: Path) -> None:
    out = tmp_path / "would-be"
    proc = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(out),
        "--retain",
        "3",
        "--dry-run",
        env=tools.env(),
    )
    assert "dry-run" in proc.stdout
    assert not out.exists(), "dry-run 不得创建备份目录"
    assert tools.calls() == "", "dry-run 不得调用外部命令"

    minio_out = tmp_path / "would-be-minio"
    proc2 = _run(
        BACKUP_MINIO,
        "--alias",
        "local",
        "--bucket",
        "contracts",
        "--out",
        _posix(minio_out),
        "--dry-run",
        env=tools.env(),
    )
    assert "dry-run" in proc2.stdout
    assert not minio_out.exists(), "dry-run 不得创建备份目录"
    assert tools.calls() == "", "dry-run 不得调用 mc"


def test_restore_drill_dry_run(tools: FakeTools, tmp_path: Path) -> None:
    pg_dir = tmp_path / "pg"
    dump, _ = _make_pg_backup(pg_dir)
    minio_backup = _make_minio_backup(tmp_path)
    report_dir = tmp_path / "report"
    before = _snapshot(tmp_path)

    proc = _run(
        RESTORE_DRILL,
        "--pg-backup",
        _posix(dump),
        "--minio-backup",
        _posix(minio_backup),
        "--target-dsn",
        DSN_URL,
        "--target-alias",
        "local",
        "--target-bucket",
        "contracts",
        "--report",
        _posix(report_dir),
        env=tools.env(),
    )
    assert "[dry-run]" in proc.stdout
    assert "supersecret" not in (proc.stdout + proc.stderr)
    assert tools.calls() == "", "dry-run 不得调用 pg_restore/psql/mc"
    assert not report_dir.exists(), "dry-run 不得写报告"
    assert _snapshot(tmp_path) == before, "dry-run 不得修改任何目标"


def test_restore_drill_execute_restores_and_verifies(
    tools: FakeTools, tmp_path: Path
) -> None:
    pg_dir = tmp_path / "pg"
    _dump, meta = _make_pg_backup(pg_dir)
    assert meta.exists()
    minio_backup = _make_minio_backup(tmp_path)
    report_dir = tmp_path / "report"

    proc = _run(
        RESTORE_DRILL,
        "--pg-backup",
        _posix(pg_dir),
        "--minio-backup",
        _posix(minio_backup),
        "--target-dsn",
        DSN_URL,
        "--target-alias",
        "local",
        "--target-bucket",
        "contracts",
        "--report",
        _posix(report_dir),
        "--execute",
        env=tools.env(FAKE_ALEMBIC_VERSION="rev-1", CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "alembic=rev-1" in proc.stdout
    calls = tools.calls()
    assert "pg_restore --list" in calls
    assert "--clean --if-exists" in calls
    assert "mc mirror" in calls
    assert "mc ls --recursive --json" in calls
    assert "supersecret" not in (proc.stdout + proc.stderr)

    report = (report_dir / f"restore_drill_{FIXED_TS}.log").read_text(encoding="utf-8")
    assert "restored_alembic=rev-1" in report
    assert "object_count=2" in report
    assert "exit_code=0" in report
    assert "supersecret" not in report


def test_restore_drill_alembic_mismatch_fails(tools: FakeTools, tmp_path: Path) -> None:
    pg_dir = tmp_path / "pg"
    _make_pg_backup(pg_dir)
    minio_backup = _make_minio_backup(tmp_path)

    proc = _run(
        RESTORE_DRILL,
        "--pg-backup",
        _posix(pg_dir),
        "--minio-backup",
        _posix(minio_backup),
        "--target-dsn",
        DSN_URL,
        "--target-alias",
        "local",
        "--target-bucket",
        "contracts",
        "--execute",
        env=tools.env(FAKE_ALEMBIC_VERSION="rev-other"),
        expect_failure=True,
    )
    assert proc.returncode == 5
    assert "alembic head" in proc.stderr


def test_restore_drill_execute_requires_targets(
    tools: FakeTools, tmp_path: Path
) -> None:
    pg_dir = tmp_path / "pg"
    _make_pg_backup(pg_dir)
    minio_backup = _make_minio_backup(tmp_path)

    proc = _run(
        RESTORE_DRILL,
        "--pg-backup",
        _posix(pg_dir),
        "--minio-backup",
        _posix(minio_backup),
        "--execute",
        expect_failure=True,
    )
    assert proc.returncode == 2
    assert "--target-dsn" in proc.stderr


def test_retention_only_target_dir(tools: FakeTools, tmp_path: Path) -> None:
    out = tmp_path / "pg-backups"
    out.mkdir()
    for ts in ("20200101T000000Z", "20210101T000000Z", "20220101T000000Z"):
        (out / f"postgres_cg_{ts}.dump").write_bytes(b"old")
        _write(out / f"postgres_cg_{ts}.meta", "component=C69\n")
    _write(out / "keep-me.txt", "keep")
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (sibling / "postgres_cg_20200101T000000Z.dump").write_bytes(b"outside")

    proc = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(out),
        "--retain",
        "1",
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "OK exit_code=0" in proc.stdout
    assert sorted(p.name for p in out.glob("postgres_cg_*.dump")) == [
        f"postgres_cg_{FIXED_TS}.dump"
    ]
    assert not (out / "postgres_cg_20200101T000000Z.meta").exists()
    assert (out / "keep-me.txt").exists()
    assert (sibling / "postgres_cg_20200101T000000Z.dump").exists()

    dry_out = tmp_path / "pg-dry"
    dry_out.mkdir()
    for ts in ("20200101T000000Z", "20210101T000000Z"):
        (dry_out / f"postgres_cg_{ts}.dump").write_bytes(b"old")
    before = _snapshot(dry_out)
    proc2 = _run(
        BACKUP_PG,
        "--dsn",
        DSN_URL,
        "--out",
        _posix(dry_out),
        "--retain",
        "1",
        "--dry-run",
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "would remove" in proc2.stdout
    assert _snapshot(dry_out) == before, "dry-run 保留策略不得删除文件"

    minio_out = tmp_path / "minio-backups"
    minio_out.mkdir()
    old = minio_out / "minio_local_contracts_20200101T000000Z"
    old.mkdir()
    keepdir = minio_out / "keepdir"
    keepdir.mkdir()
    proc3 = _run(
        BACKUP_MINIO,
        "--alias",
        "local",
        "--bucket",
        "contracts",
        "--out",
        _posix(minio_out),
        "--retain",
        "1",
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "OK exit_code=0" in proc3.stdout
    assert not old.exists()
    assert keepdir.exists()


def test_backup_minio_mirror_and_manifest(tools: FakeTools, tmp_path: Path) -> None:
    out = tmp_path / "minio-backups"
    proc = _run(
        BACKUP_MINIO,
        "--alias",
        "local",
        "--bucket",
        "contracts",
        "--out",
        _posix(out),
        env=tools.env(CG_BACKUP_TIMESTAMP=FIXED_TS),
    )
    assert "object_count=2" in proc.stdout
    target = out / f"minio_local_contracts_{FIXED_TS}"
    manifest = (target / "manifest.txt").read_text(encoding="utf-8")
    assert "source=local/contracts" in manifest
    assert "object_count=2" in manifest
    tsv = (target / "objects.tsv").read_text(encoding="utf-8")
    assert "10\ta.pdf" in tsv
    assert "20\tsub/b.docx" in tsv
    assert (target / "objects" / "a.pdf").exists()
    assert (target / "objects" / "sub" / "b.docx").exists()
    assert "mc mirror --overwrite --preserve local/contracts" in tools.calls()
