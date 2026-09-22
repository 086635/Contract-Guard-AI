"""Provider 预设表：统一 HTTP 语义服务的「端点表」（**唯一处**）。

为什么需要：决策 **D4-a** 拍板「**统一 HTTP 接口 + 配置区分多端**」——
开发期 ollama、演示期 TEI、云端 API 走的是**同一套** `CG_RAG_EMBED_*` / `CG_RAG_RERANK_*`
配置，彼此只差 `base_url` / `path` / `model` 三个值。本文件把这三件事按 provider 固化，
避免用户在每个 `.env` 里手抄（也避免"TEI 写成 `/v1/embeddings` → 404 → 静默降级"这类坑，
见 `1-6语义服务接入.md` §⑩ 避坑 2）。

三条纪律：
1. 🔴 只放**公开端点**：任何 API Key 一律走 `.env`，本文件不得出现密钥。
2. 🔴 **预设只补留空**：显式配置的值永远优先（消费方 `factory.build_embedder` / `build_reranker`）。
3. 🔴 **不得成为任何默认值**：代码默认仍是 `embed_mode=hash` / `rerank_mode=noop`（离线基线红线）；
   本表只在 `*_mode=http` 时被查询。

⚠️ **`RERANK_PRESETS["ollama"]` 是已知不可用的条目（2026-09-22 实测）**：ollama 0.34 只提供
生成与 embedding 接口，**没有 rerank 端点**（`/rerank`、`/v1/rerank`、`/api/rerank` 均返回 **404**）。
该条目仅为与 `1.C4重排器.md` §⑤ 和 `.env.example` 的既有写法保持一致而保留；本地精排请用
**TEI**（`/rerank` 真实可用）。`factory.build_reranker()` 在命中该预设时会打 WARNING 显式提示，
避免"以为配了精排、实际每次都 RERANK_FAILED"的静默降级。

定义归属（`0.§0.7`）：本文件是 `EMBED_PRESETS` / `RERANK_PRESETS` / `resolve_preset()` /
`detect_provider()` 的**唯一处**；C4 重排器**消费** `RERANK_PRESETS`（见 `1.C4重排器.md` §③），
不得在别处另写一份端点表。

对应：文件集合/codex/1.C3向量化器.md §⑤；决策 D4-a。
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Embedding 端点（键即 provider 名；`1.C3` §② 措辞里的 "openai-compatible" = 这里的 "openai"）
EMBED_PRESETS: dict[str, dict[str, str]] = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "path": "/embeddings",
        "model": "bge-m3",
    },
    "tei": {
        "base_url": "http://localhost:8080",
        "path": "/embed",
        "model": "BAAI/bge-m3",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "path": "/embeddings",
        "model": "text-embedding-3-large",
    },
}

# Rerank 端点（由 C4 消费；本卡只定义，不接线）
RERANK_PRESETS: dict[str, dict[str, str]] = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "path": "/rerank",
        "model": "bge-reranker-v2-m3",
    },
    "tei": {
        "base_url": "http://localhost:8080",
        "path": "/rerank",
        "model": "BAAI/bge-reranker-v2-m3",
    },
}

_TABLES: dict[str, dict[str, dict[str, str]]] = {
    "embed": EMBED_PRESETS,
    "rerank": RERANK_PRESETS,
}

_REQUIRED_KEYS: tuple[str, ...] = ("base_url", "path", "model")


def _table(kind: str) -> dict[str, dict[str, str]]:
    """取某类预设表。

    Args:
        kind: `embed` 或 `rerank`。

    Returns:
        对应的预设表。

    Raises:
        ValueError: `kind` 非法。
    """
    try:
        return _TABLES[kind]
    except KeyError:
        raise ValueError(
            f"不支持的预设类别：{kind!r}（可选：{sorted(_TABLES)}）"
        ) from None


def resolve_preset(kind: str, provider: str) -> dict[str, str]:
    """取某 provider 的预设端点（返回**副本**，调用方改不动 SSOT）。

    Args:
        kind: `embed` 或 `rerank`。
        provider: provider 名（大小写与首尾空白不敏感），如 `ollama` / `tei` / `openai`。

    Returns:
        含 `base_url` / `path` / `model` 三键的字典。

    Raises:
        ValueError: `kind` 或 `provider` 非法。
    """
    table = _table(kind)
    preset = table.get(provider.strip().lower())
    if preset is None:
        raise ValueError(f"未知 provider：{provider!r}（可选：{sorted(table)}）")
    return dict(preset)


def _host_port(url: str) -> str:
    """取 URL 的 `host:port`（缺端口时按 scheme 补默认端口）。

    Args:
        url: 完整 URL 或裸 host（如 `localhost:11434`）。

    Returns:
        `host:port` 形式的小写字符串。
    """
    parts = urlsplit(url if "://" in url else f"http://{url}")
    default_port = 443 if parts.scheme == "https" else 80
    return f"{(parts.hostname or '').lower()}:{parts.port or default_port}"


def detect_provider(kind: str, value: str) -> str | None:
    """由配置值反查 provider（**唯一处**）。

    为什么要"反查"而不是读配置项：本卡不允许新增 `*_provider` 配置项
    （配置唯一处是 C9 的 `rag_core/config.py`，见 `1.C3 §⑤`），
    因此 provider 只能从既有配置值推断。两种可识别形态：

    1. 值**就是** provider 名（如 `CG_RAG_EMBED_BASE_URL=ollama`）→ 直接命中；
    2. 值是 URL 且其 `host:port` 与预设表某条 `base_url` 相同
       （如 `http://localhost:11434/v1` → `ollama`、`https://api.openai.com/v1` → `openai`）→ 命中。

    其它情况（用户自己的服务地址）返回 `None`：**不套预设**，只按配置值走。

    Args:
        kind: `embed` 或 `rerank`。
        value: 配置里的 `embed_base_url` / `rerank_http_url` 取值。

    Returns:
        provider 名；无法识别时为 None。
    """
    table = _table(kind)
    token = value.strip().lower()
    if token in table:
        return token
    if not value.strip():
        return None
    wanted = _host_port(value)
    for provider, preset in table.items():
        if _host_port(preset["base_url"]) == wanted:
            return provider
    return None


__all__ = [
    "EMBED_PRESETS",
    "RERANK_PRESETS",
    "detect_provider",
    "resolve_preset",
]
