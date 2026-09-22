"""评测脚本自检（T-A8 · 组件卡 `1.C10` §⑧ / `1-7评测与召回基线.md` §⑨）。

评测脚本本身**不是** pytest 用例（`1-7 §⑨`：它会跑全量检索，不进 `testpaths`）；
这里只测它的**机制**：指标形状、确定性、门禁、标注集校验、两套标注集分列、只走生产入口。
全部在**离线默认配置**（`inmemory + hash + noop`）下跑，不需要任何外部服务。
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "eval_recall.py"
_CORPUS = _REPO_ROOT / "scripts" / "data" / "eval_corpus.jsonl"
_GOLD = _REPO_ROOT / "scripts" / "data" / "gold.jsonl"
_WEAK = _REPO_ROOT / "scripts" / "data" / "weak_gold.jsonl"


def _load_module() -> Any:
    """按路径加载评测脚本（它不在包内，不能用 import 语句）。"""
    spec = importlib.util.spec_from_file_location("eval_recall", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ev() -> Any:
    """评测脚本模块（模块级复用，避免重复 exec）。"""
    return _load_module()


@pytest.fixture()
def mini_gold(tmp_path: Path) -> Path:
    """从真实标注集里取前 3 条，构一个小标注集（跑得快）。"""
    rows = _GOLD.read_text(encoding="utf-8").splitlines()[:3]
    path = tmp_path / "mini_gold.jsonl"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    return path


@pytest.fixture()
def unrelated_gold(tmp_path: Path) -> Path:
    """构造一个**必然召回不到**的标注集（query 与语料无关），用于确定性地演示门禁失败。"""
    chunk_id = json.loads(_GOLD.read_text(encoding="utf-8").splitlines()[0])[
        "gold_chunk_ids"
    ][0]
    path = tmp_path / "unrelated.jsonl"
    path.write_text(
        json.dumps(
            {
                "query_id": "u1",
                "query": "量子计算机的退相干时间与超导约瑟夫森结的能隙测量方法",
                "gold_chunk_ids": [chunk_id],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def test_metrics_shape(ev: Any, mini_gold: Path, tmp_path: Path) -> None:
    """§⑧-1：输出含 `recall@1/5/10` + `mrr` + `empty_recall`，且走的是本仓真实语料。"""
    result = ev.evaluate(
        ev.parse_args(["--gold", str(mini_gold), "--out", str(tmp_path / "o.json")])
    )

    assert {"recall@1", "recall@3", "recall@5", "recall@10"} <= set(result["metrics"])
    assert {"mrr", "empty_recall_rate", "avg_top_score"} <= set(result["metrics"])
    assert result["meta"]["gold_count"] == 3
    assert result["meta"]["corpus_chunks"] > 100, "评测语料应已扩充到 100+ 切片"
    assert result["meta"]["config"]["embed_mode"] == "hash"


def test_deterministic_output(ev: Any, mini_gold: Path, tmp_path: Path) -> None:
    """§⑧-2 / `1-7 §⑨-1`：默认输出**逐字可复现**（不含时间戳与耗时）。"""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for target in (first, second):
        code = ev.main(["--gold", str(mini_gold), "--out", str(target)])
        assert code == 0
    assert first.read_bytes() == second.read_bytes()

    payload = json.loads(first.read_text(encoding="utf-8"))
    assert "created_at" not in payload["meta"]
    assert "avg_latency_ms" not in payload["metrics"]
    assert payload["meta"]["deterministic"] is True


def test_with_timing_adds_volatile_fields(
    ev: Any, mini_gold: Path, tmp_path: Path
) -> None:
    """`--with-timing` 才带时间戳与耗时（它是"破坏逐字复现"的那个开关）。"""
    target = tmp_path / "t.json"
    assert (
        ev.main(["--gold", str(mini_gold), "--out", str(target), "--with-timing"]) == 0
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "created_at" in payload["meta"]
    assert "avg_latency_ms" in payload["metrics"]
    assert "took_ms" in payload["per_query"][0]


def test_baseline_gate_fails(ev: Any, unrelated_gold: Path, tmp_path: Path) -> None:
    """§⑧-3 / §⑨-5：基线拔高 → 退出码 1，且给出逐条退化明细。"""
    baseline = tmp_path / "baseline.json"
    assert ev.main(["--gold", str(unrelated_gold), "--out", str(baseline)]) == 0
    current = json.loads(baseline.read_text(encoding="utf-8"))
    assert current["metrics"]["recall@5"] == 0.0, "该 query 与语料无关，不该命中"

    # 把基线拔到满分（并保持指纹一致），当前结果必然"退化"
    raised = dict(current)
    raised["metrics"] = {**current["metrics"], "recall@5": 1.0}
    raised["per_query"] = [
        {**row, "rank_of_first_gold": 1} for row in current["per_query"]
    ]
    raised_path = tmp_path / "raised.json"
    raised_path.write_text(json.dumps(raised, ensure_ascii=False), encoding="utf-8")

    gate = tmp_path / "gate.json"
    assert (
        ev.main(
            [
                "--gold",
                str(unrelated_gold),
                "--baseline",
                str(raised_path),
                "--out",
                str(gate),
            ]
        )
        == 1
    )
    payload = json.loads(gate.read_text(encoding="utf-8"))
    assert payload["regression"]["regressed"] is True
    assert payload["regression"]["regressed_queries"], "回归时必须给出逐条明细"


def test_baseline_fingerprint_mismatch_is_input_error(
    ev: Any, mini_gold: Path, tmp_path: Path
) -> None:
    """配置指纹不一致 → 退出码 2（防止拿 hash 的基线去比 http 的结果）。"""
    baseline = tmp_path / "baseline.json"
    assert ev.main(["--gold", str(mini_gold), "--out", str(baseline)]) == 0
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["meta"]["config_fingerprint"] = "deadbeef"
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert ev.main(["--gold", str(mini_gold), "--baseline", str(broken)]) == 2
    assert ev.main(
        ["--gold", str(mini_gold), "--baseline", str(broken), "--force"]
    ) in (0, 1)


def test_missing_baseline_is_not_a_crash(ev: Any, mini_gold: Path) -> None:
    """`1-7 §⑨-4`：基线文件不存在 → 明确提示 + 退出码 2，而不是崩溃。"""
    assert (
        ev.main(["--gold", str(mini_gold), "--baseline", "data/reports/__nope__.json"])
        == 2
    )


def test_gold_format_validated(ev: Any, tmp_path: Path) -> None:
    """§⑧-4：缺 `gold_chunk_ids` / 未知 chunk_id → 退出码 2 且指名行号。"""
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"query_id": "x1", "query": "缺字段"}\n', encoding="utf-8")
    assert ev.main(["--gold", str(bad)]) == 2

    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text(
        json.dumps(
            {"query_id": "x2", "query": "不存在的 id", "gold_chunk_ids": ["nope"]},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert ev.main(["--gold", str(unknown)]) == 2

    with pytest.raises(ev.InputError):
        ev.load_gold(bad)


def test_human_and_weak_reported_separately(ev: Any) -> None:
    """§⑧-5 / `AGENTS §6 DoD-4`：两套标注集**分列**，绝不合并成一个数字。"""
    assert ev.gold_source_of(_GOLD) == "human"
    assert ev.gold_source_of(_WEAK) == "weak"

    human = json.loads(
        (_REPO_ROOT / "scripts" / "data" / "baseline_gold_hash.json").read_text(
            encoding="utf-8"
        )
    )
    weak = json.loads(
        (_REPO_ROOT / "scripts" / "data" / "baseline_weak_hash.json").read_text(
            encoding="utf-8"
        )
    )
    assert human["meta"]["gold_source"] == "human"
    assert weak["meta"]["gold_source"] == "weak"
    assert human["meta"]["config_fingerprint"] != weak["meta"]["config_fingerprint"]


def test_uses_service_entrypoint(ev: Any) -> None:
    """§⑧-6：源码断言 —— 不直接 import 适配器拼流程，只走 `build_service_from_settings`。"""
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "build_service_from_settings" in source
    # 用 AST 判"真的 import 了没有"：文档字符串里提到 `rag_core.adapters`（"不得直接 import"）不算
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m.startswith("rag_core.adapters")], imported


def test_gold_filters_passed_to_request(ev: Any) -> None:
    """标注行的 `contract_type` / `source_types` 会转成 `RetrievalFilters`（否则过滤形同虚设）。"""
    filters = ev.build_filters(
        {
            "query_id": "q1",
            "query": "x",
            "gold_chunk_ids": ["a"],
            "contract_type": "PURCHASE",
            "source_types": ["STANDARD_CLAUSE", "LAW_ARTICLE"],
        }
    )
    assert (
        filters.contract_type is not None and filters.contract_type.value == "PURCHASE"
    )
    assert [s.value for s in filters.source_types or []] == [
        "STANDARD_CLAUSE",
        "LAW_ARTICLE",
    ]
    with pytest.raises(ev.InputError):
        ev.build_filters(
            {
                "query_id": "q2",
                "query": "x",
                "gold_chunk_ids": ["a"],
                "contract_type": "NOPE",
            }
        )


def test_make_weak_and_dump_chunks_are_reproducible(ev: Any, tmp_path: Path) -> None:
    """弱标注集与切片清单都可复现（弱标注集是回归门禁的输入，必须稳定）。"""
    weak_a, weak_b = tmp_path / "w_a.jsonl", tmp_path / "w_b.jsonl"
    assert ev.main(["--make-weak", str(weak_a)]) == 0
    assert ev.main(["--make-weak", str(weak_b)]) == 0
    assert weak_a.read_bytes() == weak_b.read_bytes()
    assert ev.main(["--dump-chunks", str(tmp_path / "chunks.jsonl")]) == 0


def test_repo_files_exist(ev: Any) -> None:
    """落点自证：语料 / 双标注集 / 双基线都在仓库里（基线必须入库才能回归）。"""
    for path in (
        _SCRIPT,
        _CORPUS,
        _GOLD,
        _WEAK,
        _REPO_ROOT / "scripts" / "data" / "baseline_weak_hash.json",
        _REPO_ROOT / "scripts" / "data" / "baseline_weak_http.json",
        _REPO_ROOT / "scripts" / "data" / "baseline_gold_hash.json",
        _REPO_ROOT / "scripts" / "data" / "baseline_gold_http.json",
    ):
        assert path.is_file(), f"缺少落点文件：{path}"
