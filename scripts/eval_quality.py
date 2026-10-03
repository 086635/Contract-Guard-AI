#!/usr/bin/env python
"""T-D8-4 / C79：赛题三指标**端到端真实评测**（分类准确率 / 要素 micro-F1 / 风险召回-精准）。

口径（本文件是唯一处，见 `文件集合/codex/4.C79三指标评测.md` §⑥）：

- 分类准确率 = 分类正确份数 / 有效样本数
- 要素 micro-F1 = 以 `(key, 归一化 value)` 为元素算 P/R 后取 F1
- 风险召回 / 精准 = 以 `category` 为元素：
  `recall = 命中gold / gold总数`、`precision = 命中gold / 系统输出总数`

🔴 **真实链路**：真实 RAG（Qdrant + bge-m3 + rerank）+ 真实 LLM（DeepSeek）跑审核图，
不做应答回放；`--cache` 只用于**复算**（首次必须真跑）。

阈值来自 L3 `项目代码深度阅读与分析报告.md` §1.2：85% / 80% / 75%-75%。

用法::

    python scripts/eval_quality.py --limit 3
    python scripts/eval_quality.py --cache .tmp_t/quality_cache.json --json

退出码：0 = 三指标全部达标；1 = 有未达标（**是证据，不是失败**）；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (
    _REPO_ROOT / ".venv" / "Lib" / "site-packages",
    _REPO_ROOT / "packages" / "rag-contracts",
    _REPO_ROOT / "packages" / "cg-observability",
    _REPO_ROOT / "packages" / "rag-core",
    _REPO_ROOT / "packages" / "ai-engine",
    _REPO_ROOT / "packages" / "doc-processor",
    _REPO_ROOT / "apps" / "api-gateway",
    _REPO_ROOT / "apps" / "async-worker",
):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ai_engine.agents.tools import build_rag_tool  # noqa: E402
from ai_engine.graphs import GraphDeps, build_audit_graph, new_audit_state  # noqa: E402
from ai_engine.prompts import load_default_library  # noqa: E402
from rag_contracts.element_keys import is_core_key, normalize_key  # noqa: E402

DEFAULT_GOLD = _REPO_ROOT / "scripts" / "data" / "quality_gold.jsonl"
THRESHOLDS = {"classification": 0.85, "element_f1": 0.80, "risk": 0.75}

_CLAUSE_RE = re.compile(r"(?=第[一二三四五六七八九十百零〇\d]+条)")
_STRIP_CHARS = str.maketrans({",": "", "，": "", " ": "", "￥": "", "¥": "", "元": "", "整": ""})


def normalize_value(value: object) -> str:
    """要素值归一：去空白/千分位/币种符号，ASCII 转小写。"""
    text = "" if value is None else str(value)
    return text.lower().translate(_STRIP_CHARS).strip()


def split_clauses(text: str) -> list[dict]:
    """按「第 N 条」切分合同文本；切不出则整篇作为一条。"""
    parts = [p.strip() for p in _CLAUSE_RE.split(text) if p and p.strip()]
    if not parts:
        parts = [text.strip()]
    clauses = []
    for index, body in enumerate(parts, start=1):
        label = body.splitlines()[0][:50] if body.splitlines() else body[:50]
        clauses.append(
            {
                "clause_id": f"clause-{index:04d}",
                "label": label,
                "text": body,
                "level": 1,
                "parent_id": None,
                "page_no": 1,
                "bbox": None,
            }
        )
    return clauses


def micro_f1(gold: set, predicted: set) -> tuple[float, float, float]:
    """集合级 micro P/R/F1（空集安全）。"""
    if not gold and not predicted:
        return 1.0, 1.0, 1.0
    if not gold or not predicted:
        return 0.0, 0.0, 0.0
    hit = len(gold & predicted)
    precision = hit / len(predicted)
    recall = hit / len(gold)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _element_keys(result: dict) -> set[tuple[str, str]]:
    """系统产出的**核心要素**集合（C80 口径）。

    只统计：① key 属核心词表（别名先归一）；② `isCustom=false`（非 LLM 自定义）；
    ③ value 非空。明细白名单（`payment_method` 等）与自定义 key 一律不计入
    赛题「关键要素抽取 F1」。
    """
    out: set[tuple[str, str]] = set()
    for item in result.get("elements") or []:
        if not isinstance(item, dict):
            continue
        key = normalize_key(item.get("key"))
        value = normalize_value(item.get("value"))
        is_custom = bool(item.get("isCustom", item.get("is_custom", False)))
        if not key or not value or not is_core_key(key) or is_custom:
            continue
        out.add((key, value))
    return out


def _risk_categories(result: dict) -> set[str]:
    return {
        str(item.get("category"))
        for item in (result.get("risks") or [])
        if isinstance(item, dict) and item.get("category")
    }


def run_one(graph, row: dict) -> dict:
    """跑一份合同的真实审核图，返回预测结果。"""
    clauses = split_clauses(row["text"])
    state = new_audit_state(
        row["doc_id"],
        clauses,  # type: ignore[arg-type]
        trace_id=uuid.uuid4().hex,
        tolerance="BALANCED",
    )
    final = graph.invoke(state, config={"configurable": {"thread_id": row["doc_id"]}})
    contract_type = final.get("contract_type")
    return {
        "doc_id": row["doc_id"],
        "predicted_type": getattr(contract_type, "value", contract_type),
        "contract_type": contract_type,
        "elements": list(final.get("elements") or []),
        "risks": [
            {"category": getattr(r, "category", None) and r.category.value}
            for r in (final.get("risks") or [])
        ],
        "warnings": list(final.get("warnings") or []),
    }


def evaluate(rows: list[dict], results: list[dict]) -> dict:
    """按 §⑥ 口径算三指标 + 差距分析。"""
    correct = 0
    gold_elements: set[tuple[str, str]] = set()
    pred_elements: set[tuple[str, str]] = set()
    gold_risks: set[str] = set()
    pred_risks: set[str] = set()
    misses: list[dict] = []
    confusion: dict[str, dict[str, int]] = {}

    for row, res in zip(rows, results):
        gold_type = row["contract_type"]
        pred_type = res.get("predicted_type")
        confusion.setdefault(gold_type, {})
        confusion[gold_type][str(pred_type)] = confusion[gold_type].get(str(pred_type), 0) + 1
        if pred_type == gold_type:
            correct += 1
        else:
            misses.append({"doc_id": row["doc_id"], "kind": "classification", "gold": gold_type, "predicted": pred_type})

        row_gold_el = {
            (str(e["key"]), normalize_value(e["value"])) for e in row.get("gold_elements", [])
        }
        row_pred_el = _element_keys(res)
        gold_elements |= row_gold_el
        pred_elements |= row_pred_el

        row_gold_risk = {str(r["category"]) for r in row.get("gold_risks", [])}
        row_pred_risk = _risk_categories(res)
        gold_risks |= row_gold_risk
        pred_risks |= row_pred_risk
        for missing in sorted(row_gold_risk - row_pred_risk):
            misses.append({"doc_id": row["doc_id"], "kind": "risk_missed", "category": missing})

    total = len(rows) or 1
    _, _, element_f1 = micro_f1(gold_elements, pred_elements)
    risk_precision, risk_recall, _ = micro_f1(gold_risks, pred_risks)

    metrics = {
        "sample_size": len(rows),
        "classification_accuracy": round(correct / total, 6),
        "element_micro_f1": round(element_f1, 6),
        "risk_recall": round(risk_recall, 6),
        "risk_precision": round(risk_precision, 6),
        "gold_elements": len(gold_elements),
        "predicted_elements": len(pred_elements),
        "gold_risks": len(gold_risks),
        "predicted_risks": len(pred_risks),
    }
    verdict = {
        "classification": metrics["classification_accuracy"] >= THRESHOLDS["classification"],
        "element_f1": metrics["element_micro_f1"] >= THRESHOLDS["element_f1"],
        "risk_recall": metrics["risk_recall"] >= THRESHOLDS["risk"],
        "risk_precision": metrics["risk_precision"] >= THRESHOLDS["risk"],
    }
    return {
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "verdict": verdict,
        "passed": all(verdict.values()),
        "misses": misses[:200],
        "confusion": confusion,
        "per_doc": [
            {"doc_id": r["doc_id"], "predicted_type": r.get("predicted_type")} for r in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-D8-4 赛题三指标端到端评测")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    parser.add_argument("--cache", type=Path, default=None, help="预测结果缓存（复算用）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--out", type=Path, default=None, help="结果落盘路径")
    args = parser.parse_args(argv)

    if not args.gold.is_file():
        print(f"标注集不存在：{args.gold}", file=sys.stderr)
        return 2
    rows = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit > 0:
        rows = rows[: args.limit]
    if not rows:
        print("标注集为空", file=sys.stderr)
        return 2

    started = time.perf_counter()
    try:
        graph = build_audit_graph(
            GraphDeps(rag_tool=build_rag_tool(), llm=None, prompt_lib=load_default_library())
        )
        results = [run_one(graph, row) for row in rows]
    except Exception as exc:  # noqa: BLE001 - 外部依赖不可用必须显式失败
        print(f"评测失败：{type(exc).__name__}", file=sys.stderr)
        return 2

    report = {
        "gold_file": str(args.gold),
        "elapsed_s": round(time.perf_counter() - started, 1),
        **evaluate(rows, results),
    }
    if args.cache is not None:
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        args.cache.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"样本量：{report['metrics']['sample_size']}（{report['elapsed_s']}s）")
        print(f"分类准确率   : {report['metrics']['classification_accuracy']:.2%}（阈值 {THRESHOLDS['classification']:.0%}）")
        print(f"要素 micro-F1: {report['metrics']['element_micro_f1']:.2%}（阈值 {THRESHOLDS['element_f1']:.0%}）")
        print(f"风险召回     : {report['metrics']['risk_recall']:.2%}（阈值 {THRESHOLDS['risk']:.0%}）")
        print(f"风险精准     : {report['metrics']['risk_precision']:.2%}（阈值 {THRESHOLDS['risk']:.0%}）")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
