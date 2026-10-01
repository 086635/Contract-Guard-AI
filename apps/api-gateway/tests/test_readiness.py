"""C67 live/ready 探针测试（离线：monkeypatch 四个依赖检查）。"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

REPO_ROOT = Path(__file__).resolve().parents[3]


def _patch_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    behaviours: dict[str, str],
) -> None:
    """把四个依赖检查替换成固定结论（默认 ok）。"""
    for name in main.READINESS_DEPENDENCIES:
        value = behaviours.get(name, "ok")

        async def _check(result: str = value) -> str:
            return result

        monkeypatch.setattr(main, f"_check_{name}", _check)


# --------------------------------------------------------------------------
# 断言 2：ready 四依赖检查
# --------------------------------------------------------------------------
def test_readiness_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """四项依赖全绿 → 200，且返回体逐项列出四个依赖。"""
    _patch_dependencies(monkeypatch, {})

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == set(main.READINESS_DEPENDENCIES)
    assert body["checks"] == {name: "ok" for name in main.READINESS_DEPENDENCIES}
    # 不泄露任何连接串/地址/凭据（依赖名本身是协议的一部分，允许出现）
    assert "postgresql" not in response.text
    assert "redis://" not in response.text
    assert "minioadmin" not in response.text
    assert "localhost" not in response.text
    assert "http://" not in response.text


# --------------------------------------------------------------------------
# 断言 3：依赖失败 → ready 503
# --------------------------------------------------------------------------
def test_readiness_failure_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """任一依赖失败 → 503，并指出失败项（不回显底层异常或连接信息）。"""
    _patch_dependencies(monkeypatch, {"redis": "error"})

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["checks"]["redis"] == "error"
    assert body["checks"]["database"] == "ok"


def test_readiness_probe_exception_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖检查抛异常 → 折算 error（不把异常信息透出）。"""

    async def _boom() -> str:
        raise RuntimeError("postgresql://user:secret@db/cg")

    for name in main.READINESS_DEPENDENCIES:
        monkeypatch.setattr(main, f"_check_{name}", _boom)

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert "secret" not in response.text
    assert set(response.json()["checks"].values()) == {"error"}


def test_readiness_probe_timeout_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测超时 → error（探针不阻塞）。"""
    import asyncio

    async def _slow() -> str:
        await asyncio.sleep(0.5)
        return "ok"

    monkeypatch.setattr(main, "READINESS_TIMEOUT_S", 0.01)
    for name in main.READINESS_DEPENDENCIES:
        monkeypatch.setattr(main, f"_check_{name}", _slow)

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503


# --------------------------------------------------------------------------
# 断言 4：live 不依赖外部服务
# --------------------------------------------------------------------------
def test_live_does_not_check_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖全部不可用（甚至抛异常）时 `/health/live` 仍 200；同工况 ready=503。"""

    async def _boom() -> str:
        raise RuntimeError("dependency down")

    for name in main.READINESS_DEPENDENCIES:
        monkeypatch.setattr(main, f"_check_{name}", _boom)

    with TestClient(main.app) as client:
        live = client.get("/health/live")
        legacy = client.get("/health")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert legacy.status_code == 200
    # live 返回 200 ⇒ 它从未触碰（会抛异常的）依赖检查
    assert ready.status_code == 503


def test_ready_route_uses_module_level_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """ready 必须调用模块级检查函数（便于部署侧打桩/替换）。"""
    seen: list[str] = []
    for name in main.READINESS_DEPENDENCIES:

        def _factory(dep: str) -> Callable[[], object]:
            async def _check() -> str:
                seen.append(dep)
                return "ok"

            return _check

        monkeypatch.setattr(main, f"_check_{name}", _factory(name))

    with TestClient(main.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert sorted(seen) == sorted(main.READINESS_DEPENDENCIES)


def test_docs_disabled_in_production(tmp_path: Path) -> None:
    """`CG_DISABLE_DOCS=true`（生产 overlay 设置）时公开 docs/openapi 关闭。"""
    env = os.environ.copy()
    env["CG_DISABLE_DOCS"] = "true"
    env["PYTHONPATH"] = ";".join(
        [
            str(REPO_ROOT / ".venv" / "Lib" / "site-packages"),
            str(REPO_ROOT / "packages" / "rag-contracts"),
            str(REPO_ROOT / "packages" / "cg-observability"),
            str(REPO_ROOT / "packages" / "rag-core"),
            str(REPO_ROOT / "packages" / "ai-engine"),
            str(REPO_ROOT / "packages" / "doc-processor"),
            str(REPO_ROOT / "apps" / "api-gateway"),
            env.get("PYTHONPATH", ""),
        ]
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app; print(app.docs_url, app.redoc_url, app.openapi_url)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "None None None"
