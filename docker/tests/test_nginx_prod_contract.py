"""C68 生产 nginx 契约测试（TLS / 安全头 / 限流 / SSE 不缓冲）。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.services.contract_service import MAX_UPLOAD_BYTES

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_PROD_CONF = REPO_ROOT / "docker" / "nginx.prod.conf"
PROD_OVERLAY = REPO_ROOT / "docker" / "docker-compose.prod.yml"
DEV_NGINX_CONF = REPO_ROOT / "docker" / "nginx.conf"

REQUIRED_HEADERS = (
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "frame-ancestors",
)


class _ComposeLoader(yaml.SafeLoader):
    """支持 compose 扩展标签（`!override` / `!reset`）。"""


_ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: loader.construct_sequence(node),
)
_ComposeLoader.add_constructor(
    "!reset",
    lambda loader, node: loader.construct_scalar(node),
)


def _nginx_text() -> str:
    assert NGINX_PROD_CONF.is_file()
    return NGINX_PROD_CONF.read_text(encoding="utf-8")


def _overlay() -> dict:
    return (
        yaml.load(PROD_OVERLAY.read_text(encoding="utf-8"), Loader=_ComposeLoader) or {}
    )


def _directives(text: str, name: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(f"{name} ")
    ]


# --------------------------------------------------------------------------
# 断言 1：HTTP 跳转 HTTPS
# --------------------------------------------------------------------------
def test_tls_redirect_configured() -> None:
    """80 端口 301 跳转 HTTPS；443 启用 TLS 且证书来自挂载目录。"""
    text = _nginx_text()

    assert "listen 80;" in text
    assert "return 301 https://$host$request_uri;" in text
    assert "listen 443 ssl;" in text
    assert "http2 on;" in text
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in text
    assert "ssl_certificate     /etc/nginx/certs/server.crt;" in text
    assert "ssl_certificate_key /etc/nginx/certs/server.key;" in text
    # 不关闭 TLS 校验 / 不弱化协议
    assert "ssl_verify_client off" not in text
    assert "TLSv1 " not in text and "TLSv1.1" not in text


#: 扫描仓库时直接跳过的目录（体积大且与证书无关）
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    ".tmp_t",
    "dist",
    ".pytest_cache",
    "__pycache__",
}
_CERT_SUFFIXES = {".crt", ".pem", ".key"}


def _find_certificate_files(root: Path) -> list[Path]:
    """遍历仓库查找证书/私钥文件（用 `os.walk` 剪枝，避免 rglob 扫进 node_modules）。"""
    offenders: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        base = Path(current)
        offenders.extend(
            base / name
            for name in filenames
            if Path(name).suffix.lower() in _CERT_SUFFIXES
        )
    return offenders


def test_certificate_material_not_committed() -> None:
    """证书必须由部署方挂载：仓库里不得出现证书/私钥文件。"""
    assert _find_certificate_files(REPO_ROOT) == []


# --------------------------------------------------------------------------
# 断言 2：安全头齐全
# --------------------------------------------------------------------------
def test_security_headers_configured() -> None:
    """HSTS / CSP / nosniff / Referrer-Policy / frame-ancestors 齐全，且 CSP 不用通配。"""
    text = _nginx_text()

    for header in REQUIRED_HEADERS:
        assert header in text, header
    assert "server_tokens off;" in text  # 不回显 nginx 版本

    csp = next(line for line in text.splitlines() if "Content-Security-Policy" in line)
    assert "*" not in csp, csp
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


# --------------------------------------------------------------------------
# 断言 3：上传 / API 限流生效
# --------------------------------------------------------------------------
def test_rate_limit_configured() -> None:
    """声明 API / 登录 / 上传三个限流区，并在对应路径上启用。"""
    text = _nginx_text()

    assert "limit_req_zone" in text
    assert "zone=cg_api" in text
    assert "zone=cg_login" in text
    assert "zone=cg_upload" in text

    assert "location = /api/v1/auth/login {" in text
    assert re.search(r"location ~ \^/api/v1/contracts/\(presign\|upload\)\$ \{", text)
    assert "location /api/ {" in text
    assert text.count("limit_req zone=") >= 4


# --------------------------------------------------------------------------
# 断言 4：SSE 不被缓冲
# --------------------------------------------------------------------------
def test_sse_buffering_off() -> None:
    """SSE 路径显式关闭 buffering / cache / gzip，并保留长连接超时。"""
    text = _nginx_text()

    sse_block = re.search(
        r"location ~ \^/api/v1/tasks/\[\^/\]\+/stream\$ \{(?P<body>.*?)\n    \}",
        text,
        re.S,
    )
    assert sse_block is not None, "缺少 SSE location"
    body = sse_block.group("body")

    assert "proxy_buffering off;" in body
    assert "proxy_cache off;" in body
    assert "gzip off;" in body
    assert "proxy_read_timeout 3600s;" in body
    assert 'proxy_set_header Connection "";' in body


# --------------------------------------------------------------------------
# 断言 5：请求体上限与后端一致
# --------------------------------------------------------------------------
def test_upload_body_limit_matches_app() -> None:
    """`client_max_body_size` 必须与后端 `MAX_UPLOAD_BYTES` 完全一致。"""
    text = _nginx_text()
    raw = _directives(text, "client_max_body_size")
    assert raw == ["client_max_body_size 50m;"], raw

    match = re.search(r"client_max_body_size (\d+)m;", text)
    assert match is not None
    assert int(match.group(1)) * 1024 * 1024 == MAX_UPLOAD_BYTES


# --------------------------------------------------------------------------
# 断言（附加）：compose 接线 + 生产配置真实可解析
# --------------------------------------------------------------------------
def test_prod_overlay_mounts_nginx_prod_conf_and_tls() -> None:
    """生产 overlay 用 `!override` 挂载生产配置与证书目录（替换开发配置）。"""
    nginx = _overlay()["services"]["nginx"]

    assert nginx["ports"] == ["80:80", "443:443"]
    volumes = nginx["volumes"]
    assert any(
        "nginx.prod.conf:/etc/nginx/conf.d/default.conf:ro" in item for item in volumes
    )
    assert any("/etc/nginx/certs:ro" in item for item in volumes)
    # 开发配置不再出现在生产挂载里
    assert all("nginx.conf:" not in item for item in volumes)


def test_dev_nginx_conf_kept() -> None:
    """不删除开发配置：dev nginx 仍是纯 80 端口反代。"""
    text = DEV_NGINX_CONF.read_text(encoding="utf-8")
    assert "listen 80;" in text
    assert "ssl" not in text


def test_nginx_config_parses_in_container() -> None:
    """可选真实验证：用 nginx 镜像跑 `nginx -t`（无 docker/镜像时 skip）。"""
    if shutil.which("docker") is None:
        pytest.skip("本机无 docker CLI")

    tls_dir = REPO_ROOT / ".tmp_t" / "c68tls"
    if not (tls_dir / "server.crt").is_file():
        pytest.skip("缺少自签证书夹具（.tmp_t/c68tls），跳过容器语法校验")

    try:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--add-host",
                "web:127.0.0.1",
                "--add-host",
                "api:127.0.0.1",
                "-v",
                f"{NGINX_PROD_CONF}:/etc/nginx/conf.d/default.conf:ro",
                "-v",
                f"{tls_dir}:/etc/nginx/certs:ro",
                "nginx:alpine",
                "nginx",
                "-t",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"无法运行 docker（{type(exc).__name__}）")
    if completed.returncode != 0 and "permission denied" in completed.stderr.lower():
        pytest.skip("当前环境无法访问 Docker 引擎")
    assert completed.returncode == 0, completed.stderr
    assert "test is successful" in (completed.stdout + completed.stderr)
