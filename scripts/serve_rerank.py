#!/usr/bin/env python
"""本地 rerank HTTP 服务（**开发/演示用**，实现 `RAGService` 需要的 `/rerank` 口径）。

为什么要这个脚本：`rag-core` **禁止**引入 `torch` / `sentence-transformers`（依赖红线，`0.§0.3`），
精排一律走 HTTP。生产/标准形态是 **TEI 容器**（`docker-compose` 的 `reranker`，见
`1-6语义服务接入.md` §6.2）；但 Docker 不可用时（本机实测 Docker daemon 未起），
这个脚本用**系统 Python + 本地 HF 缓存**起一个同口径服务，让"真实精排"能在离线环境验证。
它只依赖 `scripts/` 这一层，**不进任何包**，也不被 `pytest` 导入。

用法：

    # ① 起服务（默认 127.0.0.1:8081；模型走本地缓存，强制离线，不会联网）
    python scripts/serve_rerank.py
    python scripts/serve_rerank.py --port 8082 --model BAAI/bge-reranker-v2-m3

    # ② 让 rag-core 用它（两个环境变量）
    $env:CG_RAG_RERANK_MODE="http"; $env:CG_RAG_RERANK_HTTP_URL="http://127.0.0.1:8081"

    # ③ 一条命令验证（见 scripts/phase1_smoke.py 头部）
    python scripts/phase1_smoke.py

接口（与 TEI `/rerank` 对齐）：

    POST /rerank   {"query": "…", "texts": ["…", "…"]}  ->  {"scores": [0.98, 0.0003]}
    GET  /health                                         ->  {"status": "ok", "model": "…"}

⚠️ 分数口径：`CrossEncoder.predict` 对 `num_labels=1` 的 reranker 默认做 **sigmoid**，
输出已在 [0,1]（实测 `bge-reranker-v2-m3`：相关 0.98 / 无关 0.0003）。
若服务端返回的是**原始 logits**（部分 TEI 配置），`HttpReranker` 的 clamp 会把负值压成 0、
丢失排序信息 —— 那种情况需要在服务端自行 sigmoid。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# 强制离线：模型已在 HF 缓存里，避免 huggingface_hub 因联网校验而报错
# （本机实测：huggingface_hub + httpx 组合在联网校验时会抛 "client has been closed"）
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


class _Handler(BaseHTTPRequestHandler):
    """极简 `/rerank` 与 `/health` 实现。"""

    protocol_version = "HTTP/1.1"
    model: Any = None
    model_name: str = DEFAULT_MODEL

    def do_POST(self) -> None:
        """处理 `POST /rerank`：逐对打分并返回 `{"scores": [...]}`。"""
        if self.path.rstrip("/") != "/rerank":
            self._send_json({"error": f"unknown path: {self.path}"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"invalid json: {exc}"}, status=400)
            return
        query = body.get("query", "")
        texts = body.get("texts") or []
        if not isinstance(texts, list):
            self._send_json({"error": "texts must be a list"}, status=400)
            return
        started = time.perf_counter()
        scores = (
            [float(s) for s in self.model.predict([(query, t) for t in texts])]
            if texts
            else []
        )
        print(
            f"[serve_rerank] texts={len(texts)} "
            f"{round((time.perf_counter() - started) * 1000)}ms",
            flush=True,
        )
        self._send_json({"scores": scores})

    def do_GET(self) -> None:
        """健康检查：`GET /health` → 服务与模型标识。"""
        if self.path.rstrip("/") != "/health":
            self._send_json({"error": f"unknown path: {self.path}"}, status=404)
            return
        self._send_json({"status": "ok", "model": self.model_name})

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        """回写 JSON 响应。

        Args:
            payload: 响应体。
            status: HTTP 状态码。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: object) -> None:
        """静音默认访问日志（逐次耗时已由 do_POST 打印）。"""


def main() -> int:
    """起服务。

    Returns:
        进程退出码（0 正常结束）。
    """
    parser = argparse.ArgumentParser(description="本地 rerank HTTP 服务（开发/演示用）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument(
        "--port", type=int, default=8081, help="监听端口（TEI 惯例 8081）"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help="CrossEncoder 模型名/路径"
    )
    args = parser.parse_args()

    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:  # pragma: no cover - 仅在缺依赖的环境触发
        print(
            "[serve_rerank] 缺少 sentence-transformers / torch（本脚本用**系统 Python** 跑，"
            f"不要装进 rag-core 的依赖）：{exc}",
            file=sys.stderr,
        )
        return 2

    print(f"[serve_rerank] loading {args.model} …", flush=True)
    try:
        model = CrossEncoder(args.model)
    except Exception as exc:  # noqa: BLE001 - 缺权重/缓存不全时给出可照做的提示
        print(
            f"[serve_rerank] 模型加载失败：{exc}\n"
            f"  提示：需要本地 HF 缓存中已有 {args.model}（离线模式不会自动下载）。",
            file=sys.stderr,
        )
        return 2
    _Handler.model = model
    _Handler.model_name = args.model
    print(
        f"[serve_rerank] ready: POST http://{args.host}:{args.port}/rerank", flush=True
    )
    ThreadingHTTPServer((args.host, args.port), _Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
