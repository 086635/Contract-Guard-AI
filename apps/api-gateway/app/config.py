"""环境变量与系统配置（pydantic-settings，统一 CG_ 前缀）。

所有配置经环境变量 / .env 注入，禁止在代码中硬编码敏感值与基础设施地址。
对应 data_scheme.md §0 角色约束（后端工程师只读配置，不改类型）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 应用 ----
    app_name: str = "contract-guard-api"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # ---- 安全 ----
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"

    # ---- 基础设施 ----
    postgres_dsn: str = "postgresql+asyncpg://cg:cg@localhost:5432/cg"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker: str = "redis://localhost:6379/1"
    celery_backend: str = "redis://localhost:6379/2"
    qdrant_url: str = "http://localhost:6333"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "contracts"
    minio_secure: bool = False

    # ---- CORS ----
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
