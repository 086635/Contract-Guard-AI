"""Token 计数与耗时统计（实现 rag_contracts.ports.TokenCounterPort）。

Token 口径（设计文档 §3.1.1）：
- **优先** `tiktoken`（精确）；
- 不可用时按 `ceil(len(text) / 1.5)` 估算，并在 `TokenUsage.estimated=true` 标注。

`TokenMeter` 在一次调用内做**分阶段累加 + 分段计时**，
最终一次性喂给 `RAGQueryResponse.token_usage` 与 `stage_latency_ms`。
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

DEFAULT_MODEL: str = "gpt-3.5-turbo"
# 中文场景下 1 token ≈ 1.5 字符（设计文档 §3.7-6 采纳的估算口径）
DEFAULT_CHARS_PER_TOKEN: float = 1.5


class TokenCounter:
    """Token 计数器：tiktoken 优先，字符估算兜底。

    属性 `estimated` 用于回填 `TokenUsage.estimated`，
    让下游知道成本统计是精确值还是估算值。
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
        force_estimate: bool = False,
    ) -> None:
        """初始化计数器。

        Args:
            model: tiktoken 编码对应的模型名。
            chars_per_token: 估算口径（每 token 多少字符）。
            force_estimate: True 时强制走估算（便于单测与离线演示）。
        """
        self.model: str = model
        self.chars_per_token: float = max(0.1, chars_per_token)
        self._encoding: Any = None
        self._backend: str = "estimate"

        if not force_estimate:
            try:
                import tiktoken  # type: ignore[import-not-found]

                self._encoding = tiktoken.encoding_for_model(model)
                self._backend = "tiktoken"
            except Exception:  # noqa: BLE001 - 任何失败都安全回落到估算
                self._encoding = None
                self._backend = "estimate"

    @property
    def backend(self) -> str:
        """当前计数后端：`tiktoken` 或 `estimate`。"""
        return self._backend

    @property
    def estimated(self) -> bool:
        """当前是否为估算值（True 表示未启用真实 tokenizer）。"""
        return self._encoding is None

    def count(self, text: str) -> int:
        """统计单条文本的 Token 数。

        Args:
            text: 输入文本。

        Returns:
            Token 数（>=0）。
        """
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return max(1, math.ceil(len(text) / self.chars_per_token))

    def count_many(self, texts: Sequence[str]) -> int:
        """统计多条文本的 Token 总数。

        Args:
            texts: 文本序列。

        Returns:
            Token 总数。
        """
        return sum(self.count(t) for t in texts)

    def count_batch(self, texts: Sequence[str]) -> list[int]:
        """逐条统计，返回与输入等长的列表。

        Args:
            texts: 文本序列。

        Returns:
            每条文本对应的 Token 数。
        """
        return [self.count(t) for t in texts]


class TokenMeter:
    """一次调用内的 Token 累加器 + 分段计时器。

    用法：
        meter = TokenMeter()
        with meter.stage("embed"):
            vec = embedder.embed_one(q)
        meter.add("embedding", 12)          # 或 meter.add_tokens("embed", ...)
        print(meter.total, meter.latencies)
    """

    def __init__(self, counter: TokenCounter | None = None) -> None:
        """初始化计量器。

        Args:
            counter: Token 计数器；为空则自建（自动降级为估算）。
        """
        self.counter: TokenCounter = counter or TokenCounter()
        self._tokens: dict[str, int] = {}
        self._latency_ms: dict[str, int] = {}
        self._running: dict[str, float] = {}

    # ---------------- Token 累加 ----------------
    def add(self, stage: str, tokens: int) -> int:
        """为某阶段累加 Token 数。

        Args:
            stage: 阶段名（如 `embedding` / `rerank`）。
            tokens: 本次耗用的 Token 数（负数会被忽略）。

        Returns:
            该阶段累加后的总数。
        """
        if tokens <= 0:
            return self._tokens.get(stage, 0)
        self._tokens[stage] = self._tokens.get(stage, 0) + int(tokens)
        return self._tokens[stage]

    def count_text(self, stage: str, texts: str | Sequence[str]) -> int:
        """统计文本 Token 并累加到指定阶段。

        Args:
            stage: 阶段名。
            texts: 单条文本或文本序列。

        Returns:
            本次统计出的 Token 数。
        """
        if isinstance(texts, str):
            n = self.counter.count(texts)
        else:
            n = self.counter.count_many(texts)
        self.add(stage, n)
        return n

    def usage(self, stage: str) -> int:
        """读取某阶段已累加的 Token 数。"""
        return self._tokens.get(stage, 0)

    @property
    def total(self) -> int:
        """全部阶段 Token 之和。"""
        return sum(self._tokens.values())

    # ---------------- 耗时统计 ----------------
    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """分段计时上下文管理器。

        Args:
            name: 阶段名（如 `dense` / `rerank` / `build`）。

        Yields:
            None。
        """
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = int((time.perf_counter() - start) * 1000)
            self._latency_ms[name] = self._latency_ms.get(name, 0) + elapsed

    def record_latency(self, name: str, elapsed_ms: int) -> None:
        """手工登记某阶段耗时（用于无法用上下文包裹的场景）。

        Args:
            name: 阶段名。
            elapsed_ms: 耗时（毫秒）。
        """
        self._latency_ms[name] = self._latency_ms.get(name, 0) + max(0, int(elapsed_ms))

    def latency_ms(self, name: str) -> int:
        """读取某阶段累计耗时（毫秒）。"""
        return self._latency_ms.get(name, 0)

    @property
    def latencies(self) -> dict[str, int]:
        """全部分段耗时快照（可直接赋给 `stage_latency_ms`）。"""
        return dict(self._latency_ms)

    # ---------------- 汇总 ----------------
    def snapshot(self) -> dict[str, Any]:
        """输出可观测性快照。

        Returns:
            含 `tokens` / `total_tokens` / `latency_ms` / `estimated` 的字典。
        """
        return {
            "tokens": dict(self._tokens),
            "total_tokens": self.total,
            "latency_ms": dict(self._latency_ms),
            "estimated": self.counter.estimated,
            "backend": self.counter.backend,
        }


__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_MODEL",
    "TokenCounter",
    "TokenMeter",
]
