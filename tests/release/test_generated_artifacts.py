"""C74 生成物漂移/哈希与可复现输入守卫（离线）。

生成物（`schemas.py` / `types.ts` / `zod.ts`）被 `.gitignore` 忽略，
干净 checkout 需先执行 `pnpm --filter @contract-guard/schema gen`；
缺失时用例显式 skip，并在 C75/CI 的干净构建里覆盖。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI = REPO_ROOT / "packages" / "schema" / "openapi.yaml"
REPORT = REPO_ROOT / "文件集合" / "交付报告" / "T-D7生产化整改交付报告.md"

#: T-D6 knowledge maintenance（2026-10-05）：2 added read-only operations/paths.
FROZEN_OPERATION_IDS = 40
FROZEN_PATHS = 35

ARTIFACT_PATHS = (
    "packages/schema/openapi.yaml",
    "apps/api-gateway/app/generated/schemas.py",
    "apps/web-ui/src/api/generated/types.ts",
    "apps/web-ui/src/api/generated/zod.ts",
)


def _openapi_paths() -> set[str]:
    paths: set[str] = set()
    in_paths = False
    for line in OPENAPI.read_text(encoding="utf-8").splitlines():
        if line == "paths:":
            in_paths = True
            continue
        if in_paths and line.startswith("  /") and line.rstrip().endswith(":"):
            paths.add(line.strip().rstrip(":"))
    return paths


def _openapi_operation_ids() -> set[str]:
    return {
        line.strip().split("operationId: ", 1)[1].strip()
        for line in OPENAPI.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("operationId: ")
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_hashes() -> dict[str, str]:
    recorded: dict[str, str] = {}
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\| `([^`]+)` \| ([0-9a-f]{12}) \|", line.strip())
        if match:
            recorded[match.group(1)] = match.group(2)
    return recorded


def test_openapi_operation_inventory() -> None:
    """SSOT 库存冻结：operationId 与路径数量必须与发布基线一致。"""
    assert len(_openapi_operation_ids()) == FROZEN_OPERATION_IDS
    assert len(_openapi_paths()) == FROZEN_PATHS


def test_openapi_generation_drift() -> None:
    """生成物与 SSOT 零漂移：zod alias 与 types 路径必须 1:1 对齐 OpenAPI。"""
    zod = REPO_ROOT / "apps/web-ui/src/api/generated/zod.ts"
    types = REPO_ROOT / "apps/web-ui/src/api/generated/types.ts"
    if not zod.is_file() or not types.is_file():
        pytest.skip("生成物缺失：先运行 pnpm --filter @contract-guard/schema gen")

    aliases = set(re.findall(r'alias: "([^"]+)"', zod.read_text(encoding="utf-8")))
    assert aliases == _openapi_operation_ids()

    ts_paths = set(
        re.findall(r'^\s{4}"(/[^"]+)": \{', types.read_text(encoding="utf-8"), re.MULTILINE)
    )
    assert ts_paths == _openapi_paths()


def test_generated_hash_stable() -> None:
    """生成物哈希必须与交付报告登记一致；漂移即要求重新登记。"""
    recorded = _report_hashes()
    assert recorded, "交付报告缺少生成物哈希表"
    assert set(recorded) == set(ARTIFACT_PATHS), f"哈希表产物集合不符：{sorted(recorded)}"

    for relative in ARTIFACT_PATHS:
        path = REPO_ROOT / relative
        if not path.is_file():
            pytest.skip(f"生成物缺失：{relative}（先运行 pnpm --filter @contract-guard/schema gen）")
        assert _sha256(path)[:12] == recorded[relative], (
            f"{relative} 哈希漂移：需在交付报告重新登记（旧值 {recorded[relative]}）"
        )


def test_clean_checkout_build() -> None:
    """可复现输入冻结（真实干净构建由 C75/CI 执行）。"""
    for relative in (
        "pnpm-lock.yaml",
        "uv.lock",
        "pytest.ini",
        "docker/docker-compose.prod.yml",
        ".env.production.example",
    ):
        assert (REPO_ROOT / relative).is_file(), f"缺少可复现输入：{relative}"

    ini = (REPO_ROOT / "pytest.ini").read_text(encoding="utf-8")
    for required in (
        "tests/release",
        "scripts/tests",
        "docker/tests",
        "apps/async-worker/tests",
    ):
        assert required in ini, f"pytest.ini testpaths 缺少 {required}"

    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for generated in ARTIFACT_PATHS[1:]:
        assert generated in gitignore, f"生成物未 gitignore：{generated}"
