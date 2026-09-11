"""向量化适配器：实现 `rag_contracts.ports.EmbedderPort`。

两个实现，通过 `CG_RAG_EMBED_MODE` 切换：
- `HashEmbedder`（默认）：**离线兜底**。hashing trick 生成确定性伪向量，
  无需外网 / GPU / API Key。语义表达力弱于真模型，但足以跑通全链路与单测。
- `HttpEmbedder`：TEI / OpenAI 兼容接口，生产用。

为什么 HashEmbedder 可行：它把文本哈希成固定的稀疏高维向量并做 L2 归一化，
"共享词汇越多 → 余弦相似度越高"，因此能验证召回排序链路是否接通。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Sequence

# 拉丁词 / 数字 token
_LATIN_RE = re.compile(r"[a-z0-9]+")
# CJK 单字（覆盖常见汉字区）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def tokenize(text: str, *, ngram: int = 2) -> list[str]:
    """轻量分词：拉丁词 + CJK 单字 + CJK 二元组（离线，无需 jieba）。

    为什么要 CJK 二元组：中文单字召回噪声大，二元组（"违约"/"约金"）能保留语序信息。

    Args:
        text: 输入文本。
        ngram: CJK n-gram 长度（2 表示单字 + 二元组）。

    Returns:
        token 列表（已小写化）。
    """
    lowered = text.lower()
    tokens: list[str] = list(_LATIN_RE.findall(lowered))

    cjk = _CJK_RE.findall(lowered)
    tokens.extend(cjk)
    if ngram >= 2:
        tokens.extend("".join(cjk[i : i + ngram]) for i in range(max(0, len(cjk) - ngram + 1)))
    return tokens


def _stable_pair(token: str, dim: int) -> tuple[int, int]:
    """把 token 稳定映射为 (维度下标, 符号)。

    ⚠️ 不能用内置 `hash()`：Python 对 str 的哈希带随机盐（PYTHONHASHSEED），
    跨进程结果不同，会破坏"同文本同向量"的确定性要求。

    Args:
        token: 分词结果。
        dim: 向量维度。

    Returns:
        (下标, ±1 符号)。
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    index = value % dim
    sign = 1 if ((value >> 17) & 1) else -1
    return index, sign


class HashEmbedder:
    """离线哈希向量化器（hashing trick + L2 归一化）。"""

    def __init__(
        self,
        dim: int = 1024,
        *,
        model_name: str = "hash-trick-v1",
        ngram: int = 2,
    ) -> None:
        """初始化。

        Args:
            dim: 输出维度（应与 `CG_RAG_VECTOR_DIM` 一致）。
            model_name: 写入 `chunk.embedding_model` 的模型标识。
            ngram: CJK n-gram 长度。
        """
        self._dim: int = max(1, int(dim))
        self._model_name: str = model_name
        self._ngram: int = max(1, int(ngram))

    @property
    def model_name(self) -> str:
        """模型标识。"""
        return self._model_name

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        """单条向量化。

        Args:
            text: 输入文本。

        Returns:
            L2 归一化后的向量；空文本返回零向量。
        """
        vector = [0.0] * self._dim
        for token in tokenize(text, ngram=self._ngram):
            index, sign = _stable_pair(token, self._dim)
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """批量向量化（逐条处理，语义与 `embed_one` 完全一致）。

        Args:
            texts: 文本序列。

        Returns:
            与 texts 等长的向量列表。
        """
        return [self.embed_one(t) for t in texts]


class HttpEmbedder:
    """TEI / OpenAI 兼容 HTTP 向量化器（生产用）。

    兼容两种响应格式：
    - OpenAI 风格：`{"data": [{"index": 0, "embedding": [...]}, ...]}`
    - TEI 风格：`[[...], [...]]`（直接是向量数组的数组）
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "none",
        dim: int = 1024,
        batch_size: int = 32,
        timeout_ms: int = 3000,
        path: str = "/v1/embeddings",
    ) -> None:
        """初始化。

        Args:
            base_url: 服务根地址，如 `http://localhost:8080`。
            model: 模型名。
            api_key: Bearer Token；TEI 一般不需要。
            dim: 期望维度（用于校验返回）。
            batch_size: 单批文本数。
            timeout_ms: 单次请求超时（毫秒）。
            path: 接口路径。
        """
        self._base_url: str = base_url.rstrip("/")
        self._model: str = model
        self._api_key: str = api_key
        self._dim: int = dim
        self._batch_size: int = max(1, batch_size)
        self._timeout_s: float = timeout_ms / 1000.0
        self._path: str = path if path.startswith("/") else f"/{path}"

    @property
    def model_name(self) -> str:
        """模型名。"""
        return self._model

    @property
    def dim(self) -> int:
        """向量维度。"""
        return self._dim

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        """发起一次 embedding 请求。

        Args:
            texts: 本批文本。

        Returns:
            向量列表。

        Raises:
            EmbedFailedError: HTTP 失败 / 返回格式异常 / 维度不符。
        """
        from rag_contracts.errors import EmbedFailedError

        try:
            import httpx  # 延迟导入：默认路径（hash）不需要 httpx
        except ImportError as exc:  # pragma: no cover - 仅在不带 httpx 的环境触发
            raise EmbedFailedError("HttpEmbedder 需要 httpx，请安装后再启用 http 模式") from exc

        url = f"{self._base_url}{self._path}"
        headers = {"Content-Type": "application/json"}
        if self._api_key and self._api_key.lower() != "none":
            headers["Authorization"] = f"Bearer {self._api_key}"

        try:
            resp = httpx.post(
                url,
                json={"model": self._model, "input": list(texts)},
                headers=headers,
                timeout=self._timeout_s,
            )
            resp.raise_for_status()
            body: Any = resp.json()
        except EmbedFailedError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一收敛为 EmbedFailedError（可重试）
            raise EmbedFailedError(f"embedding 请求失败：{exc}") from exc

        vectors = self._parse(body)
        if len(vectors) != len(texts):
            raise EmbedFailedError(
                f"embedding 返回条数不符：期望 {len(texts)}，实际 {len(vectors)}"
            )
        for vec in vectors:
            if self._dim and len(vec) != self._dim:
                raise EmbedFailedError(
                    f"embedding 维度不符：期望 {self._dim}，实际 {len(vec)}"
                )
        return vectors

    @staticmethod
    def _parse(body: Any) -> list[list[float]]:
        """解析 OpenAI / TEI 两种响应格式。

        Args:
            body: 已 JSON 反序列化的响应体。

        Returns:
            向量列表。

        Raises:
            EmbedFailedError: 无法识别的格式。
        """
        from rag_contracts.errors import EmbedFailedError

        if isinstance(body, list):
            return [[float(x) for x in vec] for vec in body]
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                rows = sorted(
                    data,
                    key=lambda item: int(item.get("index", 0)) if isinstance(item, dict) else 0,
                )
                out: list[list[float]] = []
                for item in rows:
                    if not isinstance(item, dict) or "embedding" not in item:
                        raise EmbedFailedError("embedding 响应缺少 data[].embedding")
                    out.append([float(x) for x in item["embedding"]])
                return out
        raise EmbedFailedError("无法识别的 embedding 响应格式")

    def embed_one(self, text: str) -> list[float]:
        """单条向量化。

        Args:
            text: 输入文本。

        Returns:
            向量。
        """
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """分批向量化（按 `batch_size` 切分后顺序拼接）。

        Args:
            texts: 文本序列。

        Returns:
            与 texts 等长的向量列表。
        """
        result: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            result.extend(self._post(list(texts[start : start + self._batch_size])))
        return result


__all__ = ["HashEmbedder", "HttpEmbedder", "tokenize"]
