"""重排适配器统一出口。

设计文档 §3.2 把重排实现放在 `adapters/reranker.py`；
本模块作为**稳定导入路径**，当前只导出 Phase 1 的 `NoopReranker`，
Phase 2/3 在此追加 `CrossEncoderReranker` / `HttpReranker` 即可，
上层（factory / service）的 import 语句无需改动。
"""

from __future__ import annotations

from .noop_rerank import MODEL_NAME, NoopReranker

__all__ = ["MODEL_NAME", "NoopReranker"]
