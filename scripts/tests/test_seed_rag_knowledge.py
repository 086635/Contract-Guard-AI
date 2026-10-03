"""T-D8-1 / C76：RAG 知识库灌数脚本契约测试（离线）。

只验证**契约与纯函数**（CLI 退出码 / 计数结构 / 标题截断 / 语料分类），
不触碰 PG / Qdrant / embedding —— 真实灌库与召回由交付报告登记实测数据。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "seed_rag_knowledge.py"
CORPUS = REPO_ROOT / "scripts" / "data" / "eval_corpus.jsonl"


def _load_module():
    """按文件路径加载脚本模块（`scripts/` 不是包，故不走普通 import）。"""
    spec = importlib.util.spec_from_file_location("seed_rag_knowledge", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def seed_module():
    return _load_module()


def test_dry_run_reports_counts() -> None:
    """`--dry-run` 退出码 0 且输出 docs / 标准条款 / 法条 / 切片 计数。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["dryRun"] is True
    assert payload["docs"] == 11 + 3  # eval_corpus：11 标准条款 + 3 法条
    assert payload["standardClauses"] == 11
    assert payload["lawArticles"] == 3
    assert payload["chunks"] > 0


def test_missing_corpus_exits_2(tmp_path: Path) -> None:
    """语料不存在 → 退出码 2（参数/环境错误，不是崩溃）。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--dry-run", "--corpus", str(tmp_path / "nope.jsonl")],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert proc.returncode == 2


def test_help_exits_0() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert proc.returncode == 0
    assert "--dry-run" in proc.stdout


def test_title_takes_first_non_empty_line_and_truncates(seed_module) -> None:
    """标题取首个非空行；超长截断到 200 字符。"""
    assert seed_module._title_of("\n\n第一条 定义\n其余正文") == "第一条 定义"
    assert seed_module._title_of("   ") == "未命名条款"
    long_line = "甲" * 500
    assert len(seed_module._title_of(long_line)) == 200


def test_standard_docs_filters_by_source_type(seed_module) -> None:
    """语料按 `source_type` 分流：标准条款进 PG，其余只进向量库。"""
    docs = seed_module.eval_recall.load_corpus(CORPUS)
    standard = seed_module._standard_docs(docs)
    assert len(standard) == 11
    assert all(d.source_type == "STANDARD_CLAUSE" for d in standard)
    assert len(standard) < len(docs)
