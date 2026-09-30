"""C55 真实语义 provider 生产门禁与健康检查测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rag_contracts.errors import (
    EmbedFailedError,
    RerankFailedError,
    StoreUnavailableError,
)
from rag_core import factory as factory_module
from rag_core.config import ProductionConfigError, RagSettings
from rag_core.factory import health_check


def _settings(**overrides: Any) -> RagSettings:
    values: dict[str, Any] = {
        "vector_store": "inmemory",
        "embed_mode": "hash",
        "embed_base_url": "http://localhost:8080",
        "embed_model": "BAAI/bge-m3",
        "rerank_enabled": False,
        "rerank_mode": "noop",
        "rerank_http_url": "",
        "rerank_model": "BAAI/bge-reranker-v2-m3",
        "bm25_enabled": False,
    }
    values.update(overrides)
    return RagSettings(_env_file=None, **values)


def test_hash_rejected_in_production() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _settings(embed_mode="hash").validate_production()

    assert exc_info.value.field == "CG_RAG_EMBED_MODE"


def test_noop_rerank_rejected_in_production() -> None:
    with pytest.raises(ProductionConfigError) as exc_info:
        _settings(
            embed_mode="http",
            rerank_enabled=True,
            rerank_mode="noop",
        ).validate_production()

    assert exc_info.value.field == "CG_RAG_RERANK_MODE"


def test_http_providers_require_endpoints() -> None:
    with pytest.raises(ProductionConfigError) as embed_error:
        _settings(
            embed_mode="http",
            embed_base_url="",
            rerank_enabled=True,
            rerank_mode="http",
            rerank_http_url="http://rerank.internal",
        ).validate_production()
    assert embed_error.value.field == "CG_RAG_EMBED_BASE_URL"

    with pytest.raises(ProductionConfigError) as rerank_error:
        _settings(
            embed_mode="http",
            embed_base_url="http://embed.internal",
            rerank_enabled=True,
            rerank_mode="http",
            rerank_http_url="",
        ).validate_production()
    assert rerank_error.value.field == "CG_RAG_RERANK_HTTP_URL"


def test_valid_http_production_config_accepted() -> None:
    _settings(
        embed_mode="http",
        embed_base_url="http://embed.internal",
        rerank_enabled=True,
        rerank_mode="http",
        rerank_http_url="http://rerank.internal",
    ).validate_production()


def test_health_check_shape_offline() -> None:
    report = health_check(_settings())

    assert report == {
        "dense": "ok",
        "sparse": "disabled",
        "rerank": "disabled",
    }


def test_health_check_reports_empty_sparse_index(tmp_path: Path) -> None:
    report = health_check(
        _settings(
            bm25_enabled=True,
            bm25_dir=str(tmp_path / "bm25"),
        )
    )

    assert report["sparse"] == "empty"


def test_health_check_classifies_embedding_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_embedder(settings: RagSettings) -> Any:
        del settings
        raise EmbedFailedError("embedding unavailable")

    monkeypatch.setattr(factory_module, "build_embedder", _broken_embedder)

    assert health_check(_settings())["dense"] == "failed:EmbedFailedError"


def test_health_check_classifies_rerank_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenReranker:
        def rerank(
            self,
            query: str,
            texts: list[str],
            base_scores: list[float] | None = None,
        ) -> list[float]:
            del query, texts, base_scores
            raise RerankFailedError("rerank unavailable")

    monkeypatch.setattr(
        factory_module,
        "build_reranker",
        lambda settings: _BrokenReranker(),
    )

    report = health_check(
        _settings(
            rerank_enabled=True,
            rerank_mode="http",
            rerank_http_url="http://rerank.internal",
        )
    )

    assert report["rerank"] == "failed:RerankFailedError"


def test_embedding_dimension_guard() -> None:
    pytest.importorskip("qdrant_client")
    from rag_core.adapters.qdrant_store import QdrantVectorStore

    store = QdrantVectorStore(
        mode="local_memory",
        collection="c55-dimension-guard",
    )
    store.ensure_collection(1024)

    with pytest.raises(StoreUnavailableError) as exc_info:
        store.ensure_collection(512)

    assert exc_info.value.detail["expected_dim"] == 512
    assert exc_info.value.detail["actual_dim"] == 1024
