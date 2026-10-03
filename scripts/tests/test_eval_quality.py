"""T-D8-4 / C79：三指标评测脚本的**口径单测**（离线，不调 LLM）。

只验证指标算法与 CLI 契约；真实三指标数字由端到端评测产出并回填组件卡。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "eval_quality.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_quality", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def test_split_clauses_by_article(mod) -> None:
    text = "第一条 定义\n正文\n\n第二条 金额\n￥100 元\n\n第三条 期限\n一年"
    clauses = mod.split_clauses(text)
    assert [c["clause_id"] for c in clauses] == ["clause-0001", "clause-0002", "clause-0003"]
    assert clauses[1]["text"].startswith("第二条 金额")


def test_split_clauses_falls_back_to_single(mod) -> None:
    clauses = mod.split_clauses("没有条款编号的一段正文")
    assert len(clauses) == 1
    assert clauses[0]["clause_id"] == "clause-0001"


def test_normalize_value_strips_separators_and_symbols(mod) -> None:
    assert mod.normalize_value("￥1,200,000.00 元") == "1200000.00"
    assert mod.normalize_value("ABC") == "abc"
    assert mod.normalize_value(None) == ""


def test_micro_f1_cases(mod) -> None:
    p, r, f1 = mod.micro_f1({"a", "b"}, {"a", "b"})
    assert (p, r, f1) == (1.0, 1.0, 1.0)
    p, r, f1 = mod.micro_f1({"a"}, {"b"})
    assert (p, r, f1) == (0.0, 0.0, 0.0)
    p, r, f1 = mod.micro_f1({"a", "b"}, {"a"})
    assert p == 1.0 and r == 0.5 and abs(f1 - 2 / 3) < 1e-9
    assert mod.micro_f1(set(), set()) == (1.0, 1.0, 1.0)
    assert mod.micro_f1({"a"}, set()) == (0.0, 0.0, 0.0)


def test_evaluate_computes_three_metrics_and_gaps(mod) -> None:
    rows = [
        {
            "doc_id": "Q1",
            "contract_type": "PURCHASE",
            "gold_elements": [{"key": "amount", "value": "1,000"}],
            "gold_risks": [{"category": "PAYMENT_TERM_RISK"}],
        },
        {
            "doc_id": "Q2",
            "contract_type": "NDA",
            "gold_elements": [{"key": "party_a", "value": "甲"}],
            "gold_risks": [{"category": "VAGUE_CONFIDENTIALITY"}],
        },
    ]
    results = [
        {
            "doc_id": "Q1",
            "predicted_type": "PURCHASE",
            "elements": [{"key": "amount", "value": "1000"}],
            "risks": [{"category": "PAYMENT_TERM_RISK"}],
        },
        {
            "doc_id": "Q2",
            "predicted_type": "SALES",
            "elements": [],
            "risks": [],
        },
    ]
    report = mod.evaluate(rows, results)
    m = report["metrics"]
    assert m["sample_size"] == 2
    assert m["classification_accuracy"] == 0.5
    assert abs(m["element_micro_f1"] - 2 / 3) < 1e-6  # P=1.0, R=0.5 → F1=2/3
    assert m["risk_recall"] == 0.5
    assert m["risk_precision"] == 1.0
    assert report["verdict"]["risk_precision"] is True
    assert report["verdict"]["classification"] is False
    kinds = {item["kind"] for item in report["misses"]}
    assert kinds == {"classification", "risk_missed"}


def test_help_exits_0() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0
    assert "--limit" in proc.stdout


def test_missing_gold_exits_2(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--gold", str(tmp_path / "nope.jsonl")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 2
