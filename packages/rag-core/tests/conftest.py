"""rag-core 测试夹具：让用例**对工作区状态免疫**。

问题背景（2026-09-22 实测）：`RagSettings.bm25_dir` 默认是 `./data/bm25`（**落盘**），
而 `build_service_from_settings()` 会按配置构造 BM25 索引。于是"跑过冒烟/评测之后"，
工作区里就留着一份**别的语料**的索引，接着跑单测时融合候选会串味 ——
实测 `test_config_factory.py` 的阈值用例因此失败（候选 21 条 vs 期望 2 条）。

对策：本包用例统一把 `CG_RAG_BM25_DIR` 指向各自的 `tmp_path`，
使每个用例都在**空索引**上运行（显式传 `bm25_dir=` 的用例不受影响）。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_bm25_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 BM25 落盘目录隔离到用例自己的 tmp 目录（默认配置路径是共享的 ./data/bm25）。"""
    monkeypatch.setenv("CG_RAG_BM25_DIR", str(tmp_path / "bm25"))
