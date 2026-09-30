"""环境变量与系统配置（pydantic-settings，统一 CG_ 前缀）。

所有配置经环境变量 / .env 注入，禁止在代码中硬编码敏感值与基础设施地址。
对应 data_scheme.md §0 角色约束（后端工程师只读配置，不改类型）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 应用 ----
    runtime_env: Literal["development", "test", "production"] = "development"
    app_name: str = "contract-guard-api"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # ---- 安全 ----
    jwt_secret: str = "change-me-in-prod-0123456789abcdef"
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
    qdrant_url: str = "http://localhost:6333"
    minio_endpoint: str = "localhost:9000"
    minio_public_endpoint: str | None = None
    minio_region: str = "us-east-1"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "contracts"
    minio_secure: bool = False

    # ---- CORS ----
    cors_origins: list[str] = ["http://localhost:5173"]

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
        if not self.jwt_secret.strip() or self.jwt_secret == _DEFAULT_JWT_SECRET:
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
        if _has_default_minio_credentials(self.minio_access_key, self.minio_secret_key):
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

        for validator in (
            llm_settings or LlmSettings(),
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


__all__ = [
    "ProductionConfigError",
    "Settings",
    "get_settings",
    "settings",
]
