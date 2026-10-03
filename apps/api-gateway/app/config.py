"""环境变量与系统配置（pydantic-settings，统一 CG_ 前缀）。

所有配置经环境变量 / .env 注入，禁止在代码中硬编码敏感值与基础设施地址。
对应 data_scheme.md §0 角色约束（后端工程师只读配置，不改类型）。
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_core.config import ProductionConfigError

if TYPE_CHECKING:
    from ai_engine.llm.settings import LlmSettings
    from rag_core.config import RagSettings


_DEFAULT_JWT_SECRET = "change-me-in-prod-0123456789abcdef"
_DEFAULT_MINIO_KEYS = frozenset({"", "minioadmin"})


def _has_default_database_credentials(dsn: str) -> bool:
    """判断 DSN 是否仍使用仓库默认的 cg:cg 账号。"""
    parsed = urlparse(dsn)
    return parsed.username == "cg" and parsed.password == "cg"


def _has_default_minio_credentials(access_key: str, secret_key: str) -> bool:
    """判断 MinIO 是否仍使用默认或空口令。"""
    return (
        access_key.strip().lower() in _DEFAULT_MINIO_KEYS
        or secret_key.strip().lower() in _DEFAULT_MINIO_KEYS
    )


def _is_localhost_endpoint(endpoint: str) -> bool:
    """判断公网 endpoint 是否指向本机回环地址。"""
    value = endpoint.strip()
    if not value:
        return False
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = (parsed.hostname or parsed.path.split(":", 1)[0]).lower()
    return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


#: 生产环境必须提供的 secret 清单（C66 §⑤）：键 = `*_FILE` 字段名，值 = 环境变量名。
PRODUCTION_SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("jwt_secret_file", "CG_JWT_SECRET_FILE"),
    ("postgres_password_file", "CG_POSTGRES_PASSWORD_FILE"),
    ("minio_access_key_file", "CG_MINIO_ACCESS_KEY_FILE"),
    ("minio_secret_key_file", "CG_MINIO_SECRET_KEY_FILE"),
    ("llm_api_key_file", "CG_LLM_API_KEY_FILE"),
)


def _inject_password(dsn: str, password: str) -> str:
    """把 secret 文件里的口令注入 DSN（保留协议/主机/端口/库名；特殊字符转义）。"""
    parts = urlsplit(dsn)
    if not parts.hostname:
        return dsn
    user = parts.username or ""
    if not user:
        return dsn
    host = parts.hostname
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 应用 ----
    runtime_env: Literal["development", "test", "production"] = "development"
    app_name: str = "contract-guard-api"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    #: 生产 overlay 置 true：关闭公开 `/docs` / `/redoc` / `/openapi.json`（C67 §② 做）。
    disable_docs: bool = False

    # ---- 安全 ----
    #: 敏感值一律用 `SecretStr`：`repr(settings)` / 日志只显示 `**********`（C66 §⑥-1）。
    jwt_secret: SecretStr = SecretStr("change-me-in-prod-0123456789abcdef")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "contract-guard"
    jwt_audience: str = "contract-guard-api"
    jwt_access_ttl_seconds: int = 900
    jwt_refresh_ttl_seconds: int = 604800
    auth_lockout_threshold: int = 5
    auth_lockout_minutes: int = 15

    # ---- 基础设施 ----
    postgres_dsn: str = "postgresql+asyncpg://cg:cg@localhost:5432/cg"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker: str = "redis://localhost:6379/1"
    celery_backend: str = "redis://localhost:6379/2"
    celery_convert_queue: str = "convert"
    qdrant_url: str = "http://localhost:6333"
    minio_endpoint: str = "localhost:9000"
    minio_public_endpoint: str | None = None
    minio_region: str = "us-east-1"
    minio_access_key: SecretStr = SecretStr("minioadmin")
    minio_secret_key: SecretStr = SecretStr("minioadmin")
    minio_bucket: str = "contracts"
    minio_secure: bool = False
    #: 公开预签名 endpoint 的 TLS；内网 MinIO 仍可走 HTTP（C75 由 nginx 终止 TLS）。
    minio_public_secure: bool = False

    # ---- 生产 Secret 注入（C66）----
    #: 指向部署方挂载的 secret 文件（Docker secret / 受控文件，权限由部署方控制）。
    #: 优先级：**文件 > 显式环境变量 > 默认值**；文件存在但不可读/为空 → 启动失败。
    jwt_secret_file: str | None = None
    postgres_password_file: str | None = None
    minio_access_key_file: str | None = None
    minio_secret_key_file: str | None = None
    llm_api_key_file: str | None = None

    # ---- CORS ----
    cors_origins: list[str] = ["http://localhost:5173"]

    @model_validator(mode="after")
    def _apply_secret_files(self) -> Settings:
        """把 `CG_*_FILE` 指向的 secret 读入配置（值不进 repr/log，也不写镜像）。"""
        jwt_secret = _read_secret_file("CG_JWT_SECRET_FILE", self.jwt_secret_file)
        if jwt_secret is not None:
            self.jwt_secret = SecretStr(jwt_secret)

        access_key = _read_secret_file(
            "CG_MINIO_ACCESS_KEY_FILE", self.minio_access_key_file
        )
        if access_key is not None:
            self.minio_access_key = SecretStr(access_key)

        secret_key = _read_secret_file(
            "CG_MINIO_SECRET_KEY_FILE", self.minio_secret_key_file
        )
        if secret_key is not None:
            self.minio_secret_key = SecretStr(secret_key)

        password = _read_secret_file(
            "CG_POSTGRES_PASSWORD_FILE", self.postgres_password_file
        )
        if password is not None:
            self.postgres_dsn = _inject_password(self.postgres_dsn, password)

        llm_key = _read_secret_file("CG_LLM_API_KEY_FILE", self.llm_api_key_file)
        if llm_key is not None:
            # ai_engine 的 LlmSettings 从环境读 key；文件值落到**进程环境**（不落镜像、不落日志）
            os.environ["CG_LLM_API_KEY"] = llm_key
        return self

    # ---- 上传安全（C57）----
    #: 内容扫描档位：`off`（development/test 默认）| `clamav`（production 强制）。
    #: 消费方：`ContractService` 的首跳任务选择 + `validate_production()` 门禁。
    upload_av_mode: Literal["off", "clamav"] = "off"

    @property
    def resolved_minio_public_endpoint(self) -> str:
        """返回浏览器可访问的 MinIO endpoint，未配置时回退内部 endpoint。"""
        return self.minio_public_endpoint or self.minio_endpoint

    def validate_production(
        self,
        *,
        llm_settings: LlmSettings | None = None,
        rag_settings: RagSettings | None = None,
    ) -> None:
        """执行生产启动门禁；development/test 保留离线默认值。"""
        if self.runtime_env != "production":
            return

        from ai_engine.llm.settings import LlmSettings
        from rag_core.config import RagSettings

        violations: list[tuple[str, str]] = []
        jwt_secret = self.jwt_secret.get_secret_value()
        if not jwt_secret.strip() or jwt_secret == _DEFAULT_JWT_SECRET:
            violations.append(
                ("CG_JWT_SECRET", "production requires a non-default JWT secret")
            )
        if _has_default_database_credentials(self.postgres_dsn):
            violations.append(
                (
                    "CG_POSTGRES_DSN",
                    "production requires non-default database credentials",
                )
            )
        if _has_default_minio_credentials(
            self.minio_access_key.get_secret_value(),
            self.minio_secret_key.get_secret_value(),
        ):
            violations.append(
                (
                    "CG_MINIO_ACCESS_KEY/CG_MINIO_SECRET_KEY",
                    "production requires non-default MinIO credentials",
                )
            )
        if _is_localhost_endpoint(self.resolved_minio_public_endpoint):
            violations.append(
                (
                    "CG_MINIO_PUBLIC_ENDPOINT",
                    "production public endpoint must not be localhost",
                )
            )
        if self.upload_av_mode != "clamav":
            violations.append(
                (
                    "CG_UPLOAD_AV_MODE",
                    "production requires content antivirus scanning (clamav)",
                )
            )

        resolved_llm = llm_settings or LlmSettings()
        llm_key = getattr(resolved_llm, "api_key", None)
        llm_key_value = (
            llm_key.get_secret_value()
            if hasattr(llm_key, "get_secret_value")
            else str(llm_key or "")
        )
        if not llm_key_value.strip():
            violations.append(
                (
                    "CG_LLM_API_KEY",
                    "production requires a non-empty LLM api key (env or *_FILE)",
                )
            )

        for validator in (
            resolved_llm,
            rag_settings or RagSettings(),
        ):
            try:
                validator.validate_production()
            except ProductionConfigError as exc:
                violations.append((exc.field, exc.reason))

        if violations:
            fields = ", ".join(field for field, _ in violations)
            reasons = "; ".join(reason for _, reason in violations)
            raise ProductionConfigError(fields, reasons)


def _read_secret_file(field: str, path: str | None) -> str | None:
    """读取 secret 文件（裁剪换行）；缺失值返回 None，不可读/为空抛配置错误。

    ⚠️ 错误信息只含**字段名与异常类型**，绝不回显文件内容或路径（§⑥-1 / §⑨-2）。
    """
    if path is None or not str(path).strip():
        return None
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProductionConfigError(
            field,
            f"secret 文件不可读（{type(exc).__name__}）",
        ) from exc
    if not content:
        raise ProductionConfigError(field, "secret 文件为空")
    return content


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def main(argv: list[str] | None = None) -> int:
    """配置自检入口：`python -m app.config --check`。

    只输出**字段名与失败原因**（不回显任何 secret 值），退出码 0=通过 / 1=未通过。
    """
    parser = argparse.ArgumentParser(description="contract-guard 配置自检")
    parser.add_argument("--check", action="store_true", help="执行生产配置门禁")
    parser.parse_args(argv)
    try:
        get_settings().validate_production()
    except ProductionConfigError as exc:
        print(f"配置自检失败：{exc.field} → {exc.reason}", file=sys.stderr)
        return 1
    print("配置自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRODUCTION_SECRET_FIELDS",
    "ProductionConfigError",
    "Settings",
    "get_settings",
    "main",
    "settings",
]
