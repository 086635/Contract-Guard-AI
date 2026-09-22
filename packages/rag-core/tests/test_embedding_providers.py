"""向量化器 + provider 预设自检（T-A5 · 组件卡 `1.C3` §⑧）。

**离线部分（默认运行，不联网、不需要服务）**：
1. 预设表自证（三键齐备、都是公开端点、返回副本不改 SSOT）；
2. provider 反查（裸 provider 名 / URL 的 host:port）；
3. 显式配置优先于预设（`embed_path` / `embed_model` 不被覆盖）；
4. 维度/条数不符 → `embed.degrade` warning + `EmbedFailedError`（**不截断、不补零**）；
5. 分批：100 条 + `batch_size=32` → 4 次请求（3×32 + 1×4）；
6. 空文本 → 同维零向量、**不发请求**；超长文本 → **不做本地截断**（原样送出）；
7. 默认离线：`embed_mode == "hash"` 且 `build_embedder` 返回 `HashEmbedder`。

**真实服务（`CG_RAG_TEST_EMBED_BASE_URL` 未设置时 skip，`0.§0.6.2`）**：
8. 真实 embedding 服务返回 1024 维 + 语义相似度单调性。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import pytest

from rag_contracts.chunk import EMBEDDING_DIM
from rag_contracts.errors import EmbedFailedError
from rag_core.adapters.embedding import _LOG, HashEmbedder, HttpEmbedder, _is_blank
from rag_core.adapters.inmemory_store import cosine
from rag_core.config import RagSettings
from rag_core.factory import build_embedder
from rag_core.provider_presets import (
    EMBED_PRESETS,
    RERANK_PRESETS,
    detect_provider,
    resolve_preset,
)

_MODEL = "BAAI/bge-m3"


# ===========================================================================
# 桩 HTTP：记录请求 / 返回可控响应（不联网）
# ===========================================================================
class _FakeResponse:
    """最小 httpx.Response 替身。"""

    def __init__(self, body: Any) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self._body


class _Recorder:
    """记录每次 `httpx.post` 的入参，并按条数返回 1024 维向量。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: Any = None, headers: Any = None, **_: Any) -> Any:
        """替身请求：记录入参 + 按 input 条数返回同维向量。"""
        self.calls.append({"url": url, "json": json or {}, "headers": headers or {}})
        inputs = list((json or {}).get("input") or [])
        return _FakeResponse([[0.5] * EMBEDDING_DIM for _ in inputs])

    @property
    def batch_sizes(self) -> list[int]:
        """每批实际发送的文本条数。"""
        return [len(call["json"]["input"]) for call in self.calls]


def _embedder(**overrides: Any) -> HttpEmbedder:
    """构造一个指向桩服务的 HttpEmbedder。"""
    base: dict[str, Any] = {
        "base_url": "http://localhost:8080",
        "model": _MODEL,
        "dim": EMBEDDING_DIM,
        "batch_size": 32,
        "path": "/embed",
    }
    base.update(overrides)
    return HttpEmbedder(**base)


def _settings(**overrides: Any) -> RagSettings:
    """构造离线配置（`_env_file=None` 避免 `.env` 干扰，与既有测试同款用法）。"""
    base: dict[str, Any] = {"vector_store": "inmemory", "embed_mode": "hash"}
    base.update(overrides)
    return RagSettings(_env_file=None, **base)


# ===========================================================================
# 1. 预设表自证
# ===========================================================================
@pytest.mark.parametrize("table", [EMBED_PRESETS, RERANK_PRESETS])
def test_preset_table_is_complete(table: dict[str, dict[str, str]]) -> None:
    """§⑧ 用例 1：每个 provider 都有 `base_url` / `path` / `model` 三键。"""
    assert table, "预设表不得为空"
    for provider, preset in table.items():
        assert set(preset) == {"base_url", "path", "model"}, f"{provider} 键不全"
        assert preset["base_url"].startswith(("http://", "https://")), provider
        assert preset["path"].startswith("/"), provider
        assert preset["model"], provider


def test_preset_table_holds_no_secret() -> None:
    """§⑨ 避坑 3：预设只放公开端点，不得出现密钥字样。"""
    blob = (repr(EMBED_PRESETS) + repr(RERANK_PRESETS)).lower()
    for forbidden in ("api_key", "sk-", "secret", "password"):
        assert forbidden not in blob


def test_resolve_preset_returns_copy() -> None:
    """预设表是 SSOT：返回值必须是副本，调用方改不动它。"""
    preset = resolve_preset("embed", "ollama")
    preset["path"] = "/mutated"
    assert EMBED_PRESETS["ollama"]["path"] == "/embeddings"


def test_resolve_preset_rejects_unknown_kind_or_provider() -> None:
    """非法 `kind` / `provider` 必须显式报错（不得静默返回空表）。"""
    with pytest.raises(ValueError):
        resolve_preset("bogus", "ollama")
    with pytest.raises(ValueError) as excinfo:
        resolve_preset("embed", "nope")
    assert "ollama" in str(excinfo.value), "报错信息应列出可选 provider"


# ===========================================================================
# 2. provider 反查（不新增配置项的前提下，provider 只能从 base_url 推断）
# ===========================================================================
def test_detect_provider_by_token_and_url() -> None:
    """§⑦ 断言 1/2 的反查口径：裸 provider 名与 URL 的 host:port 都能命中。"""
    assert detect_provider("embed", "ollama") == "ollama"
    assert detect_provider("embed", " TEI ") == "tei"
    assert detect_provider("embed", "http://localhost:11434/v1") == "ollama"
    assert detect_provider("embed", "http://localhost:11434") == "ollama"
    assert detect_provider("embed", "https://api.openai.com/v1") == "openai"
    assert detect_provider("rerank", "http://localhost:8080") == "tei"
    assert detect_provider("embed", "http://my-own-tei.internal:9000") is None
    assert detect_provider("embed", "") is None


def test_preset_completion_uses_provider_token() -> None:
    """§⑦ 断言 1：`CG_RAG_EMBED_BASE_URL=ollama` → 预设端点生效。"""
    embedder = build_embedder(_settings(embed_mode="http", embed_base_url="ollama"))
    assert isinstance(embedder, HttpEmbedder)
    assert embedder._base_url == "http://localhost:11434/v1"
    assert embedder._path == "/embeddings"
    assert embedder.model_name == "bge-m3"
    assert embedder.provider == "ollama"


# ===========================================================================
# 3. 显式配置优先于预设（§⑧ 用例 2）
# ===========================================================================
def test_explicit_config_wins_over_preset() -> None:
    """显式 `embed_path` / `embed_model` 不被预设覆盖（预设只补留空）。"""
    embedder = build_embedder(
        _settings(
            embed_mode="http",
            embed_base_url="tei",
            embed_path="/my/embed",
            embed_model="my-model",
        )
    )
    assert embedder._base_url == "http://localhost:8080"  # 裸 provider 名 → 预设补 URL
    assert embedder._path == "/my/embed"  # 显式值优先
    assert embedder.model_name == "my-model"  # 显式值优先


def test_custom_endpoint_gets_no_preset() -> None:
    """用户自定义端点：不套预设，path / model 用配置默认值。"""
    embedder = build_embedder(
        _settings(embed_mode="http", embed_base_url="http://my-host:9000")
    )
    assert embedder._base_url == "http://my-host:9000"
    assert embedder._path == "/v1/embeddings"
    assert embedder.provider == "http"  # 未识别到 provider 时的埋点值


# ===========================================================================
# 4. 维度 / 条数不符 → warning + 报错（不截断、不补零）
# ===========================================================================
def test_dim_mismatch_degrades(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """§⑧ 用例 3：桩服务返回 8 维 → `embed.degrade` warning + `EmbedFailedError`。

    ⚠️ 卡片 §⑤ 原文写「该批返回 `None`」；但 `EmbedderPort` 是**冻结**协议
    （`embed_batch -> list[list[float]]`），返回 `None` 会让上层聚合直接炸掉。
    故按同一意图改用**既有错误类型**表达"本批失败"：`EmbedFailedError` 是
    `TransientRagError`，`service.retrieve()` 已把它降级为 `degraded=True`。
    """
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse([[0.1] * 8]))
    monkeypatch.setattr(_LOG, "propagate", True)
    with caplog.at_level(logging.WARNING), pytest.raises(EmbedFailedError) as excinfo:
        _embedder().embed_batch(["正常长度的一段文本"])

    assert "维度不符" in str(excinfo.value)
    degraded = [
        r for r in caplog.records if getattr(r, "cg_stage", "") == "embed.degrade"
    ]
    assert degraded, "维度不符必须留痕（embed.degrade）"
    fields = degraded[0].cg_fields
    assert fields["reason"] == "dim_mismatch"
    assert fields["expected_dim"] == EMBEDDING_DIM
    assert fields["actual_dim"] == 8


def test_count_mismatch_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回条数不符同样必须报错（不得静默错位配对）。"""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse([[0.1] * EMBEDDING_DIM])
    )
    with pytest.raises(EmbedFailedError):
        _embedder().embed_batch(["a", "b"])


def test_request_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络失败 → `EmbedFailedError`（可重试），不得裸抛 httpx 异常。"""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(EmbedFailedError):
        _embedder().embed_one("文本")


# ===========================================================================
# 5. 分批（§⑧ 用例 4）
# ===========================================================================
def test_batch_splits_by_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """100 条 + `batch_size=32` → 4 次请求（3×32 + 1×4）。"""
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder.post)
    texts = [f"第 {i} 条 逾期付款违约金" for i in range(100)]

    vectors = _embedder(batch_size=32).embed_batch(texts)

    assert len(vectors) == 100
    assert recorder.batch_sizes == [32, 32, 32, 4]


def test_empty_batch_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """空列表 → 直接返回 `[]`，不发请求。"""
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder.post)
    assert _embedder().embed_batch([]) == []
    assert recorder.calls == []


# ===========================================================================
# 6. 空文本 / 超长的确定性口径
# ===========================================================================
def test_is_blank_covers_whitespace() -> None:
    """空文本判定：`""` / 纯空白 / None 都算空。"""
    assert _is_blank("")
    assert _is_blank("   ")
    assert _is_blank("\n\t")
    assert not _is_blank("a")


def test_blank_text_returns_zero_vector_without_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空文本 → 同维零向量且**不发请求**（服务端普遍不接受空 input）。"""
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder.post)

    vectors = _embedder().embed_batch(["", "   ", "正常文本"])

    assert len(vectors) == 3
    assert vectors[0] == [0.0] * EMBEDDING_DIM
    assert vectors[1] == [0.0] * EMBEDDING_DIM
    assert vectors[2] != [0.0] * EMBEDDING_DIM
    assert recorder.batch_sizes == [1], "只应把非空文本发出去"


def test_overlong_text_is_not_truncated_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超长文本**不做本地截断**：原样送出（口径见 `embed_batch` docstring）。

    `CG_RAG_MAX_QUERY_CHARS` 的消费方是 Agent 工具层「防线 1」（T-B4），
    `rag-core` 不实现切分 —— 本层若偷偷截断，就会出现"两处截断口径不一致"。
    """
    recorder = _Recorder()
    monkeypatch.setattr(httpx, "post", recorder.post)
    long_text = "违约金" * 5000

    _embedder().embed_batch([long_text])

    assert recorder.calls[0]["json"]["input"] == [long_text]


# ===========================================================================
# 7. 默认离线（红线）
# ===========================================================================
def test_default_is_offline() -> None:
    """§⑧ 用例 6：默认 `hash`，不被 provider 预设污染。"""
    settings = RagSettings()
    assert settings.embed_mode == "hash"
    assert isinstance(build_embedder(settings), HashEmbedder)


def test_default_hash_embedder_matches_blank_policy() -> None:
    """同口径校验：`HashEmbedder` 对空文本同样给零向量（两实现行为一致）。"""
    assert HashEmbedder(dim=EMBEDDING_DIM).embed_one("") == [0.0] * EMBEDDING_DIM


# ===========================================================================
# 8. 真实服务（未设 CG_RAG_TEST_EMBED_BASE_URL 时 skip）
# ===========================================================================
_REAL_BASE_URL = os.getenv("CG_RAG_TEST_EMBED_BASE_URL")
# ⚠️ CPU 上真实模型"首次推理 / 空闲后被卸载再加载"实测可达 ~34 s（`1-6 §⑩` 避坑 3），
#    故真实用例的超时单独放宽（可用 `CG_RAG_TEST_EMBED_TIMEOUT_MS` 覆盖）。
_REAL_TIMEOUT_MS = int(os.getenv("CG_RAG_TEST_EMBED_TIMEOUT_MS") or "120000")


@pytest.mark.skipif(
    not _REAL_BASE_URL,
    reason="未设置 CG_RAG_TEST_EMBED_BASE_URL，跳过真实 embedding 服务用例（保持离线基线）",
)
def test_real_service_embed() -> None:
    """§⑧ 用例 5：真实服务返回 1024 维，且语义相似度有单调性。"""
    provider = detect_provider("embed", _REAL_BASE_URL or "")
    preset = resolve_preset("embed", provider) if provider else {}
    embedder = HttpEmbedder(
        base_url=_REAL_BASE_URL or "",
        model=os.getenv("CG_RAG_TEST_EMBED_MODEL") or preset.get("model", _MODEL),
        path=os.getenv("CG_RAG_TEST_EMBED_PATH") or preset.get("path", "/embed"),
        dim=EMBEDDING_DIM,
        timeout_ms=_REAL_TIMEOUT_MS,
        provider=provider or "",
    )
    one = embedder.embed_one("逾期付款违约金")
    assert len(one) == EMBEDDING_DIM
    batch = embedder.embed_batch(["逾期付款违约金", "不可抗力", "保密义务"])
    assert [len(v) for v in batch] == [EMBEDDING_DIM] * 3
    # 语义自证：同主题文本的相似度应高于无关文本（这条才证明"真语义"生效）
    assert cosine(one, batch[0]) > cosine(one, batch[2])
