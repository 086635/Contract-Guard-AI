"""HTTP Cross-Encoder 精排：实现 `rag_contracts.ports.RerankerPort`。

设计约束：rag-core 禁止引入 sentence-transformers / torch，故本地模型一律走 HTTP 服务。
失败语义：HTTP 异常/超时 → 抛 RerankFailedError（retryable），由 RAGService._rerank_safe
降级为 base_scores 并置 fallback_reason=RERANK_FAILED（不中断检索）。

对应：RAG搭建框架.md §6.4 / §8.7。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx

from rag_contracts.citation import clamp_score
from rag_contracts.errors import RerankFailedError


class HttpReranker:
    """HTTP 精排器（C1 之后为三参契约）。"""

    def __init__(
        self,
        url: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        *,
        api_key: str = "none",
        timeout_ms: int = 3000,
        query_path: str = "/rerank",
    ) -> None:
        """初始化。

        Args:
            url: 重排服务根地址。
            model: 模型名。
            api_key: Bearer Token；"none" 时不带鉴权头。
            timeout_ms: 单次请求超时（毫秒）。
            query_path: 接口路径。

        Raises:
            RerankFailedError: url 为空（配置缺失属于不可恢复，直接抛）。
        """
        if not url:
            raise RerankFailedError("rerank http url 未配置", detail={"url": url})
        self._url = url.rstrip("/")
        self._model = model
        self._timeout_s = max(0.1, timeout_ms / 1000.0)
        self._path = query_path if query_path.startswith("/") else f"/{query_path}"
        self._headers = (
            {"Authorization": f"Bearer {api_key}"} if api_key != "none" else {}
        )

    @property
    def model_name(self) -> str:
        """模型名（埋点 `retrieve.rerank` 的 model 字段）。"""
        return self._model

    def rerank(
        self,
        query: str,
        texts: Sequence[str],
        base_scores: Sequence[float] | None = None,
    ) -> list[float]:
        """对 (query, text) 逐对打分。

        Args:
            query: 查询文本。
            texts: 候选文本。
            base_scores: 上游得分；本实现不使用（保留仅为满足 RerankerPort 三参契约）。

        Returns:
            与 texts 等长、已 clamp 到 [0,1] 的分数列表。

        Raises:
            RerankFailedError: 请求失败或返回结构不合法（retryable，触发降级）。
        """
        del base_scores  # 服务端自行打分，无需上游分数
        if not texts:
            return []
        payload = {"model": self._model, "query": query, "texts": list(texts)}
        started = time.perf_counter()
        try:
            resp = httpx.post(
                f"{self._url}{self._path}",
                json=payload,
                headers=self._headers,
                timeout=self._timeout_s,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            raise RerankFailedError(
                "rerank 服务调用失败",
                detail={
                    "model": self._model,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": str(exc),
                },
            ) from exc

        raw = body.get("scores") if isinstance(body, dict) else None
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise RerankFailedError(
                "rerank 返回结构不合法",
                detail={
                    "expected": len(texts),
                    "got": (len(raw) if isinstance(raw, list) else None),
                },
            )
        # clamp 到 [0,1]：复用 rag_contracts.citation.clamp_score。
        # 【偏离规范】§6.4 样例为内联 `max(0.0, min(1.0, float(score)))`，此处故意不逐字对齐：
        # 内联写法对 NaN 会返回 1.0（脏分被抬成满分、击穿 0.35 阈值，违反红线 4），
        # clamp_score 正确归 0.0；同时避免重复定义 clamp 规则。请勿按规范逐字改回。
        # 非数值分数收敛为 RerankFailedError，与 §6.4"结构不合法 → RerankFailedError"一致。
        try:
            return [clamp_score(float(score)) for score in raw]
        except (TypeError, ValueError) as exc:
            raise RerankFailedError(
                "rerank 返回分数非数值",
                detail={"model": self._model, "scores": repr(raw)[:200]},
            ) from exc


__all__ = ["HttpReranker"]
