"""HTTP 精排自检（设计文档 T03-5 / §8.7 / B.2.3）。"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import pytest

from rag_contracts.errors import RerankFailedError
from rag_core import factory as factory_module
from rag_core.adapters.http_reranker import HttpReranker
from rag_core.adapters.noop_rerank import NoopReranker
from rag_core.config import RagSettings
from rag_core.factory import build_reranker
from rag_core.provider_presets import RERANK_PRESETS, resolve_preset


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


# ===========================================================================
# C4 / T-A6：provider 预设装配（离线，不联网）
# ===========================================================================
def _settings(**overrides: Any) -> RagSettings:
    """构造离线配置（`_env_file=None` 避免 `.env` 干扰，与既有测试同款用法）。"""
    base: dict[str, Any] = {"vector_store": "inmemory", "rerank_mode": "noop"}
    base.update(overrides)
    return RagSettings(_env_file=None, **base)


def test_rerank_preset_fills_blank_path_and_model() -> None:
    """§⑧-1（口径见 §⑤）：`CG_RAG_RERANK_HTTP_URL=tei` → 预设补全 url / path / model。

    ⚠️ 卡片原文用例名是 `test_rerank_preset_resolves_when_url_blank`（"留空 → 用预设"），
    但同卡 §⑤ 的行为表写的是「`rerank_mode=http` 且 **url 为空** → warning + `NoopReranker`（既有行为，保留）」
    —— 二者矛盾。以 §⑤（接口规格）为准：**留空 URL 仍走 noop**（见下一条用例），
    预设只补「url 给了 provider 名 / 命中预设」时的 `path` 与 `model`。
    """
    # ① provider 名形态：url / model 都来自预设。模型名故意用 ollama 的 `bge-reranker-v2-m3`
    #    （与 `RagSettings.rerank_model` 的字段默认值 `BAAI/bge-reranker-v2-m3` **不同**），
    #    否则"补没补"看不出来。
    ollama = build_reranker(_settings(rerank_mode="http", rerank_http_url="ollama"))
    assert isinstance(ollama, HttpReranker)
    assert ollama._url == "http://localhost:11434/v1"
    assert ollama._path == "/rerank"
    assert ollama.model_name == "bge-reranker-v2-m3"

    # ② tei 预设：URL 与路径同样来自预设
    tei = build_reranker(_settings(rerank_mode="http", rerank_http_url="tei"))
    assert isinstance(tei, HttpReranker)
    assert tei._url == "http://localhost:8080"
    assert tei._path == "/rerank"

    # ③ 显式配置优先（预设只补留空）
    explicit = build_reranker(
        _settings(
            rerank_mode="http",
            rerank_http_url="tei",
            rerank_path="/wrong",
            rerank_model="my-model",
        )
    )
    assert explicit._path == "/wrong"
    assert explicit.model_name == "my-model"


def test_rerank_explicit_url_wins() -> None:
    """§⑧-2：显式 URL / path / model 不被预设覆盖；自定义端点不套预设。"""
    reranker = build_reranker(
        _settings(
            rerank_mode="http",
            rerank_http_url="http://my-rerank:9000",
            rerank_path="/score",
            rerank_model="my-model",
        )
    )
    assert isinstance(reranker, HttpReranker)
    assert reranker._url == "http://my-rerank:9000"
    assert reranker._path == "/score"
    assert reranker.model_name == "my-model"


def test_blank_url_still_falls_back_to_noop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§⑤ 红线：`rerank_mode=http` 但 **url 留空** → warning + `NoopReranker`（不套预设）。"""
    monkeypatch.setattr(factory_module._LOG, "propagate", True)
    with caplog.at_level(logging.WARNING):
        reranker = build_reranker(_settings(rerank_mode="http", rerank_http_url=""))
    assert isinstance(reranker, NoopReranker)
    assert any("rerank_http_url" in r.getMessage() for r in caplog.records)


def test_ollama_rerank_preset_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """实测纠偏：ollama **没有** rerank 端点（404）→ 命中该预设时必须打 WARNING。

    预设表条目为兼容 `1.C4 §⑤` / `.env.example` 而保留，但"静默降级"是 `1-6 §⑩` 避坑 1/4
    点名的头号坑，故在装配处显式告警。
    """
    monkeypatch.setattr(factory_module._LOG, "propagate", True)
    with caplog.at_level(logging.WARNING):
        reranker = build_reranker(
            _settings(rerank_mode="http", rerank_http_url="http://localhost:11434/v1")
        )
    assert isinstance(reranker, HttpReranker), "仍应构造出来（让日志与降级路径可见）"
    assert any("ollama" in r.getMessage() for r in caplog.records)


def test_rerank_preset_table_is_tei_and_ollama() -> None:
    """预设表自证（C3 定义、C4 消费）：`tei` 的路径是 `/rerank`。"""
    assert resolve_preset("rerank", "tei")["path"] == "/rerank"
    assert set(RERANK_PRESETS) >= {"tei", "ollama"}


# ===========================================================================
# C4 / T-A6：真实 rerank 服务（未设 CG_RAG_TEST_RERANK_URL 时 skip）
# ===========================================================================
_REAL_RERANK_URL = os.getenv("CG_RAG_TEST_RERANK_URL")
_REAL_TIMEOUT_MS = int(os.getenv("CG_RAG_TEST_RERANK_TIMEOUT_MS") or "120000")

_MONOTONIC_TRIPLES: tuple[tuple[str, str, str], ...] = (
    (
        "逾期付款违约金怎么算",
        "买方逾期支付违约金的，按日万分之五计算",
        "本合同适用中华人民共和国法律",
    ),
    (
        "逾期付款违约金怎么算",
        "逾期付款的，每逾期一日按未付款项的万分之五支付违约金",
        "因不可抗力无法履行的，双方互不承担违约责任",
    ),
    ("保密义务", "保密义务在合同终止后继续有效三年", "质保期为验收合格之日起十二个月"),
    (
        "合同解除条件",
        "甲方有权在提前三十日书面通知后解除本合同",
        "发票类型为增值税专用发票，税率百分之十三",
    ),
    (
        "管辖与争议解决",
        "合同争议提交北京仲裁委员会仲裁解决",
        "验收标准以双方确认的技术协议为准",
    ),
    (
        "质保期",
        "质保期为验收合格之日起十二个月",
        "任何一方不得未经对方书面同意转让本合同项下权利义务",
    ),
    (
        "交付地点",
        "甲方向乙方采购设备，交付地点为北京市海淀区",
        "保密义务在合同终止后继续有效三年",
    ),
    (
        "保险责任",
        "保险费用由乙方承担，保额不低于合同总价的百分之十",
        "发票类型为增值税专用发票，税率百分之十三",
    ),
    (
        "知识产权保证",
        "乙方保证所供货物不存在权利瑕疵与知识产权纠纷",
        "甲方有权在提前三十日书面通知后解除本合同",
    ),
    (
        "价款支付方式",
        "第六百二十六条 买受人应当按照约定的数额和支付方式支付价款。",
        "第五百九十条 当事人一方因不可抗力不能履行合同的，应当及时通知对方。",
    ),
)


@pytest.mark.skipif(
    not _REAL_RERANK_URL,
    reason="未设置 CG_RAG_TEST_RERANK_URL，跳过真实 rerank 服务用例（保持离线基线）",
)
def test_real_service_rerank_monotonic() -> None:
    """§⑧-3：真实服务返回长度正确，且 10 组三元组里"更相关文本分更高"≥ 9 组。

    ⚠️ 该断言只在真实服务上才有意义：桩服务不构成"精排真的生效"的证据（`1-6 §⑩` 避坑 4）。
    """
    reranker = HttpReranker(url=_REAL_RERANK_URL or "", timeout_ms=_REAL_TIMEOUT_MS)
    passed = 0
    details: list[str] = []
    for query, relevant, irrelevant in _MONOTONIC_TRIPLES:
        scores = reranker.rerank(query, [relevant, irrelevant])
        assert len(scores) == 2
        assert all(0.0 <= s <= 1.0 for s in scores)
        if scores[0] > scores[1]:
            passed += 1
        else:
            details.append(f"{query}: {scores[0]:.4f} <= {scores[1]:.4f}")
    assert passed >= 9, f"单调性抽检仅 {passed}/10 通过：{details}"
