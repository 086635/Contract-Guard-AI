"""C66 生产 Secret 与配置注入测试。

离线覆盖：secret 文件读取/覆盖、缺失即失败、默认口令拒绝、repr/model_dump 不回显 secret、
生产模板无真实值、自检 CLI 的失败原因只含字段名。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_engine.llm.settings import LlmSettings
from app.config import (
    PRODUCTION_SECRET_FIELDS,
    ProductionConfigError,
    Settings,
)
from rag_core.config import RagSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = REPO_ROOT / ".env.production.example"
PROD_COMPOSE_PATH = REPO_ROOT / "docker" / "docker-compose.prod.yml"


def _valid_llm(**overrides: object) -> LlmSettings:
    values: dict[str, object] = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_key": "llm-test-key",
        "base_url": "https://api.deepseek.com/v1",
        "allow_external_data": True,
    }
    values.update(overrides)
    return LlmSettings(_env_file=None, **values)


def _valid_rag(**overrides: object) -> RagSettings:
    values: dict[str, object] = {
        "embed_mode": "http",
        "embed_base_url": "https://embed.example.com/v1/embeddings",
        "embed_api_key": "embed-test-key",
        "rerank_enabled": True,
        "rerank_mode": "http",
        "rerank_http_url": "https://rerank.example.com/rerank",
        "rerank_api_key": "rerank-test-key",
    }
    values.update(overrides)
    return RagSettings(_env_file=None, **values)


def _gateway(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "runtime_env": "production",
        "jwt_secret": "production-jwt-secret-0123456789abcdef",
        "postgres_dsn": "postgresql+asyncpg://cg:strong-password@db.internal:5432/cg",
        "minio_access_key": "prod-access",
        "minio_secret_key": "prod-secret",
        "minio_public_endpoint": "minio.example.com:9000",
        "upload_av_mode": "clamav",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _validate(settings: Settings, **overrides: object) -> None:
    settings.validate_production(
        llm_settings=overrides.get("llm_settings", _valid_llm()),
        rag_settings=overrides.get("rag_settings", _valid_rag()),
    )


def _write_secret(path: Path, value: str) -> Path:
    path.write_text(f"{value}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 断言 1：缺任一生产 secret 启动失败
# --------------------------------------------------------------------------
def test_production_secret_required(tmp_path: Path) -> None:
    """`*_FILE` 指向的文件缺失/为空 → 立刻失败（消息里带字段名，不回显任何 secret 值）。

    注：secret 文件解析发生在 `Settings` 构造期（早于任何业务代码），pydantic 会把
    validator 抛出的 `ProductionConfigError` 包装成 `ValidationError`——消息仍保留字段名。
    """
    missing = tmp_path / "nope" / "jwt_secret"
    with pytest.raises((ProductionConfigError, ValidationError)) as exc_info:
        Settings(_env_file=None, jwt_secret_file=str(missing))
    assert "CG_JWT_SECRET_FILE" in str(exc_info.value)

    empty = tmp_path / "empty_secret"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises((ProductionConfigError, ValidationError)) as empty_info:
        Settings(_env_file=None, minio_secret_key_file=str(empty))
    assert "CG_MINIO_SECRET_KEY_FILE" in str(empty_info.value)

    # production 缺 LLM key（既没有 *_FILE 也没有环境变量）→ 门禁失败
    with pytest.raises(ProductionConfigError) as llm_info:
        _validate(_gateway(), llm_settings=_valid_llm(api_key=""))
    assert "CG_LLM_API_KEY" in llm_info.value.field


def test_production_secret_files_are_read_and_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5 个 `*_FILE` 都能生效：覆盖内联值，且口令注入 DSN（不落镜像、不进 repr）。"""
    monkeypatch.delenv("CG_LLM_API_KEY", raising=False)
    settings = Settings(
        _env_file=None,
        runtime_env="production",
        jwt_secret="inline-jwt-should-be-replaced",
        minio_access_key="inline-access",
        minio_secret_key="inline-secret",
        postgres_dsn="postgresql+asyncpg://cg:inline@db.internal:5432/cg",
        jwt_secret_file=str(_write_secret(tmp_path / "jwt", "secret-from-file-jwt")),
        minio_access_key_file=str(
            _write_secret(tmp_path / "access", "access-from-file")
        ),
        minio_secret_key_file=str(_write_secret(tmp_path / "minio", "minio-from-file")),
        postgres_password_file=str(
            _write_secret(tmp_path / "pg", "p@ss:word/with#chars")
        ),
        llm_api_key_file=str(_write_secret(tmp_path / "llm", "llm-from-file")),
    )

    assert settings.jwt_secret.get_secret_value() == "secret-from-file-jwt"
    assert settings.minio_access_key.get_secret_value() == "access-from-file"
    assert settings.minio_secret_key.get_secret_value() == "minio-from-file"
    # 口令按 URL 规则转义后注入 DSN，其余部分保持
    assert "p%40ss%3Aword%2Fwith%23chars" in settings.postgres_dsn
    assert settings.postgres_dsn.endswith("@db.internal:5432/cg")
    # LLM key 落到进程环境，供 ai_engine 的 LlmSettings 消费
    assert os.environ["CG_LLM_API_KEY"] == "llm-from-file"


# --------------------------------------------------------------------------
# 断言 2：默认口令被拒
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"jwt_secret": "change-me-in-prod-0123456789abcdef"}, "CG_JWT_SECRET"),
        ({"jwt_secret": ""}, "CG_JWT_SECRET"),
        (
            {"postgres_dsn": "postgresql+asyncpg://cg:cg@localhost:5432/cg"},
            "CG_POSTGRES_DSN",
        ),
        ({"minio_access_key": "minioadmin"}, "CG_MINIO_ACCESS_KEY/CG_MINIO_SECRET_KEY"),
        ({"minio_secret_key": "minioadmin"}, "CG_MINIO_ACCESS_KEY/CG_MINIO_SECRET_KEY"),
    ],
)
def test_default_secret_rejected(overrides: dict[str, object], field: str) -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _validate(_gateway(**overrides))

    assert field in exc_info.value.field
    # 失败原因只给字段名与说明，绝不回显口令本身
    assert "minioadmin" not in str(exc_info.value).lower()
    assert "change-me-in-prod" not in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# 断言 3：secret 不进入日志（repr / str / dump）
# --------------------------------------------------------------------------
def test_secret_not_in_settings_repr(tmp_path: Path) -> None:
    """`repr/str/model_dump` 都不含 secret 明文（SecretStr 生效）。"""
    settings = Settings(
        _env_file=None,
        jwt_secret=str(
            _write_secret(tmp_path / "jwt", "repr-jwt-secret-value").read_text()
        ).strip(),
        minio_secret_key=str(
            _write_secret(tmp_path / "minio", "repr-minio-secret-value").read_text()
        ).strip(),
    )

    text = "\n".join(
        [
            repr(settings),
            str(settings),
            str(settings.model_dump()),
            str(settings.model_dump_json()),
        ]
    )

    assert "repr-jwt-secret-value" not in text
    assert "repr-minio-secret-value" not in text
    assert "**********" in text  # SecretStr 的掩码


# --------------------------------------------------------------------------
# 断言 4：模板无真实值
# --------------------------------------------------------------------------
def test_prod_env_template_has_no_secret() -> None:
    """生产模板只放占位：敏感键要么为空、要么是 `<...>` 形式。"""
    assert TEMPLATE_PATH.is_file()
    content = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "minioadmin" not in content.lower()
    assert "change-me-in-prod" not in content.lower()

    secret_key_re = re.compile(
        r"^(CG_\w*(?:SECRET|PASSWORD|API_KEY|ACCESS_KEY)\w*)=", re.IGNORECASE
    )
    inspected = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if not secret_key_re.match(f"{key}="):
            continue
        inspected += 1
        assert value == "" or (value.startswith("<") and value.endswith(">")), stripped
    assert (
        inspected >= 5
    )  # CG_JWT_SECRET / CG_*_FILE / LLM/MINIO/EMBED/RERANK 密钥都在检查范围内


def test_prod_compose_overlay_mounts_secret_files() -> None:
    """compose 叠加层用 Docker secret 文件挂载，且只暴露 *_FILE 路径（不写明文）。"""
    assert PROD_COMPOSE_PATH.is_file()
    content = PROD_COMPOSE_PATH.read_text(encoding="utf-8")

    assert "secrets:" in content
    for _, env_name in PRODUCTION_SECRET_FIELDS:
        assert env_name in content
        assert "/run/secrets/" in content
    # 明文 secret 不允许出现在 compose 里（只用 *_FILE 路径）
    assert "minioadmin" not in content
    assert "CG_JWT_SECRET:" not in content


# --------------------------------------------------------------------------
# 自检 CLI：只给字段名
# --------------------------------------------------------------------------
def test_config_self_check_cli_reports_field_names_only(tmp_path: Path) -> None:
    """`python -m app.config --check`：失败时退出码 1 且只输出字段名与原因。"""
    env = os.environ.copy()
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
    env["CG_RUNTIME_ENV"] = "production"
    env["CG_JWT_SECRET"] = "super-secret-jwt-value-should-not-print"
    env["CG_LLM_API_KEY"] = ""

    completed = subprocess.run(
        [sys.executable, "-m", "app.config", "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert completed.returncode == 1
    assert "CG_JWT_SECRET" in completed.stderr or "CG_POSTGRES_DSN" in completed.stderr
    # 自检输出绝不回显 secret 值
    assert (
        "super-secret-jwt-value-should-not-print"
        not in completed.stdout + completed.stderr
    )

    env["CG_RUNTIME_ENV"] = "development"
    ok = subprocess.run(
        [sys.executable, "-m", "app.config", "--check"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert ok.returncode == 0, ok.stderr
