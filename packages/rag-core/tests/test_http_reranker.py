"""HTTP 精排自检（设计文档 T03-5 / §8.7 / B.2.3）。"""

from __future__ import annotations

import httpx
import pytest

from rag_contracts.errors import RerankFailedError
from rag_core.adapters.http_reranker import HttpReranker


class _FakeResp:
    """最小 httpx.Response 替身。"""

    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._body


def test_empty_url_raises() -> None:
    """url 为空 → RerankFailedError（配置缺失不可恢复）。"""
    with pytest.raises(RerankFailedError):
        HttpReranker(url="")


def test_empty_texts_short_circuits() -> None:
    """texts 为空 → 返回 []，不发请求。"""
    reranker = HttpReranker(url="http://localhost:8080")
    assert reranker.rerank("q", []) == []


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock 返回 {"scores": [0.9, 0.1]} → 原样返回。"""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResp({"scores": [0.9, 0.1]})
    )
    reranker = HttpReranker(url="http://localhost:8080")
    assert reranker.rerank("q", ["a", "b"]) == [0.9, 0.1]


def test_clamp_to_unit_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """mock 返回 [-5, 9] → clamp 到 [0.0, 1.0]。"""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"scores": [-5, 9]}))
    reranker = HttpReranker(url="http://localhost:8080")
    assert reranker.rerank("q", ["a", "b"]) == [0.0, 1.0]


def test_network_error_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """端口不可达 → 抛 RerankFailedError 且 retryable=True。"""

    def boom(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", boom)
    reranker = HttpReranker(url="http://localhost:8080")
    with pytest.raises(RerankFailedError) as excinfo:
        reranker.rerank("q", ["a"])
    assert excinfo.value.retryable is True


def test_length_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回长度不符 → 抛 RerankFailedError。"""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResp({"scores": [0.1]}))
    reranker = HttpReranker(url="http://localhost:8080")
    with pytest.raises(RerankFailedError):
        reranker.rerank("q", ["a", "b"])


class _StatusErrorResp:
    """`raise_for_status()` 抛 HTTPStatusError 的替身（模拟非 2xx）。"""

    def raise_for_status(self) -> None:
        request = httpx.Request("POST", "http://localhost:8080/rerank")
        raise httpx.HTTPStatusError(
            "500 Internal Server Error",
            request=request,
            response=httpx.Response(500, request=request),
        )


def test_http_5xx_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 2xx（raise_for_status 抛出）→ 抛 RerankFailedError 且 retryable=True。"""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _StatusErrorResp())
    reranker = HttpReranker(url="http://localhost:8080")
    with pytest.raises(RerankFailedError) as excinfo:
        reranker.rerank("q", ["a"])
    assert excinfo.value.retryable is True


def test_non_numeric_score_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """分数非数值（null / 非数字字符串）→ RerankFailedError，不得以 TypeError 逃逸。"""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResp({"scores": [None, "bad"]})
    )
    reranker = HttpReranker(url="http://localhost:8080")
    with pytest.raises(RerankFailedError):
        reranker.rerank("q", ["a", "b"])


def test_nan_score_clamped_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """NaN 分必须归 0.0（红线 4），不得被抬成 1.0 击穿缺失检测阈值。"""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResp({"scores": [float("nan"), 0.5]})
    )
    reranker = HttpReranker(url="http://localhost:8080")
    assert reranker.rerank("q", ["a", "b"]) == [0.0, 0.5]


def test_model_name_property() -> None:
    """model_name 属性供埋点。"""
    reranker = HttpReranker(url="http://localhost:8080", model="my-reranker")
    assert reranker.model_name == "my-reranker"
