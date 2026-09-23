"""rag-core 测试夹具：让用例**对工作区状态免疫**。

问题背景（2026-09-22 实测）：`RagSettings.bm25_dir` 默认是 `./data/bm25`（**落盘**），
而 `build_service_from_settings()` 会按配置构造 BM25 索引。于是"跑过冒烟/评测之后"，
工作区里就留着一份**别的语料**的索引，接着跑单测时融合候选会串味 ——
实测 `test_config_factory.py` 的阈值用例因此失败（候选 21 条 vs 期望 2 条）。

对策：本包用例统一把 `CG_RAG_BM25_DIR` 指向各自的 `tmp_path`，
使每个用例都在**空索引**上运行（显式传 `bm25_dir=` 的用例不受影响）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolated_bm25_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 BM25 落盘目录隔离到用例自己的 tmp 目录（默认配置路径是共享的 ./data/bm25）。"""
    monkeypatch.setenv("CG_RAG_BM25_DIR", str(tmp_path / "bm25"))


@pytest.fixture()
def fake_pymilvus(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """注入假的 `pymilvus` 模块，使 `MilvusVectorStore` **离线可构造**。

    为什么需要：`MilvusVectorStore.__init__` 会立刻构造 `MilvusClient`（**构造即建连**），
    本机没有 Milvus 服务时必然抛 `StoreUnavailableError` —— 而"工厂的 milvus 分支返回了
    正确的类型"这条接线断言必须能离线跑（`0-C1 §⑦-1/§⑧-1`）。装个假的 SDK 既验证了
    装配参数，又不碰网络。

    Returns:
        形如 `{"client": <假客户端>}` 的容器：`client.kwargs` 是构造时收到的参数。
    """

    class _FakeClient:
        """假 MilvusClient：只记录构造参数。"""

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    holder: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> Any:
        holder["client"] = _FakeClient(**kwargs)
        return holder["client"]

    module = types.ModuleType("pymilvus")
    module.MilvusClient = _make_client  # type: ignore[attr-defined]
    module.DataType = types.SimpleNamespace(  # type: ignore[attr-defined]
        VARCHAR="VARCHAR", FLOAT_VECTOR="FLOAT_VECTOR", JSON="JSON"
    )
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return holder
