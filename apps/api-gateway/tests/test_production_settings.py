"""C53 生产配置校验与启动门禁测试。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_engine.llm.settings import LlmSettings
from app import main
from app.config import ProductionConfigError, Settings
from rag_core.config import RagSettings


def _valid_llm_settings(**overrides: Any) -> LlmSettings:
    values: dict[str, Any] = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_key": "test-key",
        "base_url": "https://api.deepseek.com/v1",
        "allow_external_data": True,
    }
    values.update(overrides)
    return LlmSettings(_env_file=None, **values)


def _valid_rag_settings(**overrides: Any) -> RagSettings:
    values: dict[str, Any] = {
        "embed_mode": "http",
        "embed_base_url": "https://embed.example.com/v1/embeddings",
        "embed_api_key": "test-key",
        "rerank_enabled": True,
        "rerank_mode": "http",
        "rerank_http_url": "https://rerank.example.com/rerank",
        "rerank_api_key": "test-key",
    }
    values.update(overrides)
    return RagSettings(_env_file=None, **values)


def _valid_gateway_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "runtime_env": "production",
        "jwt_secret": "production-jwt-secret-0123456789abcdef",
        "postgres_dsn": ("postgresql+asyncpg://cg:strong-password@db.internal:5432/cg"),
        "minio_access_key": "prod-access",
        "minio_secret_key": "prod-secret",
        "minio_public_endpoint": "minio.example.com:9000",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _validate(
    settings: Settings,
    *,
    llm_settings: LlmSettings | None = None,
    rag_settings: RagSettings | None = None,
) -> None:
    settings.validate_production(
        llm_settings=llm_settings or _valid_llm_settings(),
        rag_settings=rag_settings or _valid_rag_settings(),
    )


def test_production_rejects_offline_provider() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _validate(
            _valid_gateway_settings(),
            llm_settings=_valid_llm_settings(provider="offline"),
        )

    assert "CG_LLM_PROVIDER" in exc_info.value.field


def test_production_rejects_hash_embedding() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _validate(
            _valid_gateway_settings(),
            rag_settings=_valid_rag_settings(embed_mode="hash"),
        )

    assert "CG_RAG_EMBED_MODE" in exc_info.value.field


def test_production_rejects_noop_reranker() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _validate(
            _valid_gateway_settings(),
            rag_settings=_valid_rag_settings(rerank_mode="noop"),
        )

    assert "CG_RAG_RERANK_MODE" in exc_info.value.field


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"jwt_secret": ""}, "CG_JWT_SECRET"),
        (
            {"jwt_secret": "change-me-in-prod-0123456789abcdef"},
            "CG_JWT_SECRET",
        ),
        (
            {"postgres_dsn": ("postgresql+asyncpg://cg:cg@localhost:5432/cg")},
            "CG_POSTGRES_DSN",
        ),
        (
            {"minio_access_key": "minioadmin"},
            "CG_MINIO_ACCESS_KEY/CG_MINIO_SECRET_KEY",
        ),
        (
            {"minio_secret_key": "minioadmin"},
            "CG_MINIO_ACCESS_KEY/CG_MINIO_SECRET_KEY",
        ),
    ],
)
def test_production_rejects_default_secrets(
    overrides: dict[str, Any],
    field: str,
) -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _validate(_valid_gateway_settings(**overrides))

    assert field in exc_info.value.field
    assert "minioadmin" not in str(exc_info.value).lower()
    assert "change-me-in-prod" not in str(exc_info.value).lower()


def test_production_rejects_localhost_public_endpoint() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _validate(
            _valid_gateway_settings(
                minio_public_endpoint="http://localhost:9000",
            )
        )

    assert "CG_MINIO_PUBLIC_ENDPOINT" in exc_info.value.field


def test_production_accepts_explicit_non_offline_config() -> None:
    _validate(_valid_gateway_settings())


def test_development_keeps_offline_defaults() -> None:
    development = Settings(_env_file=None, runtime_env="development")
    llm_settings = LlmSettings(_env_file=None)
    rag_settings = RagSettings(_env_file=None)

    assert llm_settings.provider == "offline"
    assert rag_settings.embed_mode == "hash"
    assert rag_settings.rerank_mode == "noop"

    development.validate_production(
        llm_settings=llm_settings,
        rag_settings=rag_settings,
    )


def test_startup_runs_production_validation_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _StubSettings:
        app_name = "contract-guard-api"

        def validate_production(self) -> None:
            calls.append("validated")

    monkeypatch.setattr(main, "settings", _StubSettings())

    with TestClient(main.app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert calls == ["validated"]


def test_startup_propagates_production_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubSettings:
        app_name = "contract-guard-api"

        def validate_production(self) -> None:
            raise ProductionConfigError(
                "CG_LLM_PROVIDER",
                "production forbids offline provider",
            )

    monkeypatch.setattr(main, "settings", _StubSettings())

    with pytest.raises(ProductionConfigError):
        with TestClient(main.app):
            pass
