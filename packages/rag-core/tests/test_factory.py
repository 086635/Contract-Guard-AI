"""工厂接线自检（设计文档 T03-2 / T03-6 / B.2.6）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rag_contracts.ports import SparseStorePort
from rag_core.adapters import HttpReranker, NoopReranker
from rag_core.adapters.bm25_store import BM25Store
from rag_core.config import RagSettings
from rag_core.factory import (
    build_reranker,
    build_service_from_settings,
    build_sparse_store,
)


def _settings(tmp_path: Path, **overrides: Any) -> RagSettings:
    """构造落盘到临时目录的配置。"""
    return RagSettings(_env_file=None, bm25_dir=str(tmp_path / "bm25"), **overrides)


def test_build_sparse_store_returns_bm25(tmp_path: Path) -> None:
    """默认配置返回 BM25Store，且满足 SparseStorePort。"""
    store = build_sparse_store(_settings(tmp_path))
    assert isinstance(store, BM25Store)
    assert isinstance(store, SparseStorePort)


def test_build_sparse_store_disabled(tmp_path: Path) -> None:
    """bm25_enabled=False 时返回 None（策略自动退化 DENSE_ONLY）。"""
    assert build_sparse_store(_settings(tmp_path, bm25_enabled=False)) is None


def test_build_reranker_noop_by_default(tmp_path: Path) -> None:
    """默认 rerank_mode=noop → NoopReranker。"""
    assert isinstance(build_reranker(_settings(tmp_path)), NoopReranker)


def test_build_reranker_http(tmp_path: Path) -> None:
    """rerank_mode=http 且 url 非空 → HttpReranker。"""
    cfg = _settings(
        tmp_path, rerank_mode="http", rerank_http_url="http://localhost:8080"
    )
    assert isinstance(build_reranker(cfg), HttpReranker)


def test_build_reranker_http_without_url_falls_back(tmp_path: Path) -> None:
    """rerank_mode=http 但 url 为空 → 回退 NoopReranker（降级而非崩溃）。"""
    cfg = _settings(tmp_path, rerank_mode="http", rerank_http_url="")
    assert isinstance(build_reranker(cfg), NoopReranker)


def test_build_reranker_local_falls_back(tmp_path: Path) -> None:
    """rerank_mode=local（torch 被禁）→ 回退 NoopReranker。"""
    cfg = _settings(tmp_path, rerank_mode="local")
    assert isinstance(build_reranker(cfg), NoopReranker)


def test_build_service_has_sparse_store(tmp_path: Path) -> None:
    """build_service_from_settings 的 sparse_store 非 None（T03-2 DoD）。"""
    service = build_service_from_settings(_settings(tmp_path))
    assert service.sparse_store is not None
    assert isinstance(service.sparse_store, SparseStorePort)


@pytest.mark.parametrize("enabled", [False, True])
def test_build_service_respects_bm25_enabled(tmp_path: Path, enabled: bool) -> None:
    """sparse_store 是否为空与 bm25_enabled 一致。"""
    service = build_service_from_settings(_settings(tmp_path, bm25_enabled=enabled))
    assert (service.sparse_store is not None) is enabled
