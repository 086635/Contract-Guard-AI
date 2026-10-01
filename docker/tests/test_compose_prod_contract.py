"""C67 生产 compose 契约测试（端口收敛 / secret 挂载 / 探针与门禁）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"
PROD_COMPOSE = REPO_ROOT / "docker" / "docker-compose.prod.yml"

PUBLIC_PORTS = {"80", "443"}
#: 生产 overlay 里必须显式收敛端口的内网服务
INTERNAL_SERVICES = ("postgres", "redis", "qdrant", "minio", "api", "web", "clamav")
SECRET_NAMES = (
    "cg_jwt_secret",
    "cg_postgres_password",
    "cg_minio_access_key",
    "cg_minio_secret_key",
    "cg_llm_api_key",
)


class _ComposeLoader(yaml.SafeLoader):
    """支持 compose 扩展标签（`!override` / `!reset`）的 SafeLoader。"""


def _construct_sequence(loader: yaml.SafeLoader, node: yaml.Node) -> list:
    return loader.construct_sequence(node)


def _construct_scalar(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_sequence)
_ComposeLoader.add_constructor("!reset", _construct_scalar)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}


def _write_secrets(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in (
        "jwt_secret",
        "postgres_password",
        "minio_access_key",
        "minio_secret_key",
        "llm_api_key",
    ):
        (directory / name).write_text(f"placeholder-{name}", encoding="utf-8")
    return directory


def _effective_config(tmp_path: Path) -> dict | None:
    """真实合并后的 compose 配置；没有 docker CLI 时返回 None（用例跳过）。"""
    if shutil.which("docker") is None:
        return None
    env = {**os.environ, "CG_SECRETS_DIR": str(_write_secrets(tmp_path / "secrets"))}
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(PROD_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env=env,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return json.loads(completed.stdout)


# --------------------------------------------------------------------------
# 断言 1：prod 端口限制
# --------------------------------------------------------------------------
def test_only_public_ports(tmp_path: Path) -> None:
    """生产只公开 nginx 的 80/443；内网服务在 overlay 里显式清空端口。"""
    overlay = _load(PROD_COMPOSE)
    services = overlay.get("services", {})
    for name in INTERNAL_SERVICES:
        assert name in services, name
        assert services[name].get("ports") == [], name
    assert services["nginx"]["ports"] == ["80:80", "443:443"]

    merged = _effective_config(tmp_path)
    if merged is None:
        pytest.skip("docker CLI 不可用，跳过合并后的端口校验")
    published: dict[str, list[str]] = {}
    for name, service in merged["services"].items():
        ports = [str(item.get("published")) for item in service.get("ports", [])]
        if ports:
            published[name] = ports
    assert set(published) == {"nginx"}
    assert set(published["nginx"]) <= PUBLIC_PORTS


def test_development_compose_kept(tmp_path: Path) -> None:
    """不删除 development compose：基础文件仍发布本地开发端口。"""
    base = _load(BASE_COMPOSE)
    assert base["services"]["postgres"]["ports"] == ["5432:5432"]
    assert base["services"]["nginx"]["ports"] == ["8080:80"]

    merged = _effective_config(tmp_path)
    if merged is None:
        pytest.skip("docker CLI 不可用，跳过合并校验")
    # 叠加 prod 之后基础端口必须被收敛掉
    assert merged["services"]["postgres"].get("ports", []) == []


# --------------------------------------------------------------------------
# 断言 2：prod compose 使用 secrets
# --------------------------------------------------------------------------
def test_prod_compose_uses_secrets() -> None:
    """secret 通过 Docker secret 文件挂载（`*_FILE` 路径），compose 内不出现明文。"""
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    overlay = _load(PROD_COMPOSE)

    assert set(overlay["secrets"]) == set(SECRET_NAMES)
    for name in SECRET_NAMES:
        assert f"/run/secrets/{name}" in text
    for env_name in (
        "CG_JWT_SECRET_FILE",
        "CG_POSTGRES_PASSWORD_FILE",
        "CG_MINIO_ACCESS_KEY_FILE",
        "CG_MINIO_SECRET_KEY_FILE",
        "CG_LLM_API_KEY_FILE",
    ):
        assert env_name in text

    # 明文 secret 不得出现在编排里（只用 *_FILE 路径）
    assert "minioadmin" not in text
    assert "CG_JWT_SECRET:" not in text
    assert "CG_MINIO_SECRET_KEY:" not in text

    merged = _effective_config(Path(os.environ.get("TEMP", "/tmp")) / "cg67")
    if merged is None:
        pytest.skip("docker CLI 不可用，跳过合并后的 secret 校验")
    api = merged["services"]["api"]
    assert api["environment"]["CG_JWT_SECRET_FILE"] == "/run/secrets/cg_jwt_secret"
    assert {item["source"] for item in api.get("secrets", [])} == set(SECRET_NAMES)


# --------------------------------------------------------------------------
# 断言（附加）：探针/门禁/资源
# --------------------------------------------------------------------------
def test_prod_overlay_sets_live_probe_and_docs_gate() -> None:
    """api 健康检查走 `/health/live`；生产关闭公开 docs；服务带 restart/日志/资源上限。"""
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    overlay = _load(PROD_COMPOSE)
    api = overlay["services"]["api"]

    assert "health/live" in text
    assert api["environment"]["CG_DISABLE_DOCS"] == "true"
    assert api["restart"] == "unless-stopped"
    assert api["logging"]["driver"] == "json-file"
    assert "limits" in api["deploy"]["resources"]

    for name in (
        "postgres",
        "redis",
        "qdrant",
        "minio",
        "worker",
        "worker-convert",
        "nginx",
    ):
        service = overlay["services"][name]
        assert service["restart"] == "unless-stopped", name
        assert "limits" in service["deploy"]["resources"], name
