#!/usr/bin/env python
"""T-A8 / C10 检索评测：Recall@K · MRR · 回归门禁（**唯一处**：CLI + 指标口径 + 门禁）。

口径（`1-7评测与召回基线.md` §⑤，**写死不得中途更换**）：

- `Recall@K`（命中即算 / hit-rate）：`gold_chunk_ids ∩ topK != ∅` 记 1，否则 0；对全部 query 取均值。
- `MRR`：`1 / rank_of_first_gold`（topK 内没有 gold 则 0）；对全部 query 取均值。
- 另记：`empty_recall_rate`（降级为"缺失标准对照"的比例）、`avg_top_score`、耗时。

评测对象是**产品路径**：一律经 `build_service_from_settings()`（与 api / worker 同一套装配），
本脚本**不得**直接 import `rag_core.adapters` 拼流程。

典型用法（离线即可跑）：

    # ① 生成弱标注集（每切片一条，零人工；用于回归门禁）
    python scripts/eval_recall.py --make-weak scripts/data/weak_gold.jsonl
    # ② 看语料切片清单（人工标注 gold 时用它拿 chunk_id）
    python scripts/eval_recall.py --dump-chunks data/reports/chunks.jsonl
    # ③ 离线基线（hash + noop + inmemory，不需要任何外部服务）
    python scripts/eval_recall.py --gold scripts/data/weak_gold.jsonl \
        --out scripts/data/baseline_weak_hash.json
    # ④ 回归门禁：与基线比 recall@5，跌幅 > 0.05 即退出码 1
    python scripts/eval_recall.py --gold scripts/data/weak_gold.jsonl \
        --baseline scripts/data/baseline_weak_hash.json
    # ⑤ 真实语义（ollama bge-m3 + 本地 rerank 服务，见 scripts/serve_rerank.py）
    $env:CG_RAG_VECTOR_STORE="qdrant"; $env:CG_RAG_QDRANT_MODE="local_memory"
    $env:CG_RAG_EMBED_MODE="http"; $env:CG_RAG_EMBED_BASE_URL="http://localhost:11434/v1"
    $env:CG_RAG_EMBED_TIMEOUT_MS="300000"
    $env:CG_RAG_RERANK_MODE="http"; $env:CG_RAG_RERANK_HTTP_URL="http://127.0.0.1:8081"
    python scripts/eval_recall.py --gold scripts/data/gold.jsonl --strategy HYBRID_RRF \
        --out scripts/data/baseline_gold_http.json

退出码：`0` 达标且不低于基线；`1` 回归退化（recall@5 跌幅 > 0.05）；`2` 输入/配置错误。

两大纪律：
1. 🔴 **两套标注集分列**：人工（`gold.jsonl`）与弱标注（`weak_gold.jsonl`）**分别跑、分别登记**，
   输出里用 `meta.gold_source` 标明来源，**永不合并成一个数字**（`AGENTS.md §6 DoD-4`）。
2. 🔴 **确定性**：默认输出**不含时间戳与耗时**（`--with-timing` 才带），
   因此同一配置两次运行可逐字比对（弱标注集回归门禁依赖这一点）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 让脚本直接从仓库根目录运行：把 workspace 内的包加入 sys.path（无需 pip install）
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("rag-contracts", "cg-observability", "rag-core"):
    _path = _REPO_ROOT / "packages" / _pkg
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from rag_contracts.chunk import ContractChunk  # noqa: E402
from rag_contracts.enums import ContractType, RetrieveStrategy, SourceType  # noqa: E402
from rag_contracts.ports import VectorRecord  # noqa: E402
from rag_contracts.query import RAGQueryRequest, RetrievalFilters  # noqa: E402
from rag_core.chunking import SimpleChunker, SourceDoc  # noqa: E402
from rag_core.config import RagSettings  # noqa: E402
from rag_core.factory import build_service_from_settings  # noqa: E402

DEFAULT_CORPUS = _REPO_ROOT / "scripts" / "data" / "eval_corpus.jsonl"
DEFAULT_OUT_DIR = _REPO_ROOT / ".tmp_t" / "eval_bm25"
DEFAULT_CUTOFFS: tuple[int, ...] = (1, 3, 5, 10)
GATE_TOLERANCE = 0.05  # recall@5 允许波动（1-7 §7.4）


class InputError(Exception):
    """输入 / 配置错误（对应退出码 2）。"""


def silence_logs() -> None:
    """静音既有埋点日志（`rag_core` / `cg_observability`）。

    `--json` 模式要求 stdout **只有结果 JSON**（便于 `| python -c "json.load(sys.stdin)"`
    与逐字比对）；而这些 logger 在自己的 handler 上直写 stdout、且 `propagate=False`，
    所以只能在级别上静音。
    """
    for name, obj in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(obj, logging.Logger) and name.split(".")[0] in (
            "rag_core",
            "cg_observability",
            "scripts",
        ):
            obj.setLevel(logging.CRITICAL)


# ---------------------------------------------------------------------------
# 语料与标注集
# ---------------------------------------------------------------------------
def load_corpus(path: Path) -> list[SourceDoc]:
    """严格加载语料（**任何一行解析失败即报错**，不像冒烟脚本那样静默跳过）。

    Args:
        path: JSONL 语料路径。

    Returns:
        SourceDoc 列表。

    Raises:
        InputError: 文件不存在、为空或存在无法解析的行。
    """
    if not path.exists():
        raise InputError(f"语料文件不存在：{path}")
    docs: list[SourceDoc] = []
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            docs.append(SourceDoc.from_dict(json.loads(line)))
        except Exception as exc:
            raise InputError(f"语料第 {line_no} 行解析失败：{exc}") from exc
    if not docs:
        raise InputError(f"语料为空：{path}")
    return docs


def load_gold(path: Path) -> list[dict[str, Any]]:
    """严格加载标注集（必填 `query_id` / `query` / `gold_chunk_ids`）。

    Args:
        path: gold JSONL 路径。

    Returns:
        标注行列表（原样字典，便于保留 `note` 等可选字段）。

    Raises:
        InputError: 文件不存在、为空、行解析失败或缺必填字段。
    """
    if not path.exists():
        raise InputError(f"标注集不存在：{path}")
    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(f"标注集第 {line_no} 行不是合法 JSON：{exc}") from exc
        if not isinstance(item, dict):
            raise InputError(f"标注集第 {line_no} 行必须是对象")
        for field in ("query_id", "query", "gold_chunk_ids"):
            if not item.get(field):
                raise InputError(f"标注集第 {line_no} 行缺少必填字段 {field!r}")
        if not isinstance(item["gold_chunk_ids"], list):
            raise InputError(f"标注集第 {line_no} 行的 gold_chunk_ids 必须是数组")
        items.append(item)
    if not items:
        raise InputError(f"标注集为空：{path}")
    return items


def gold_source_of(path: Path) -> str:
    """判定标注集来源（**文件级**区分，输出里绝不合并）。

    Args:
        path: 标注集路径。

    Returns:
        `weak`（文件名含 weak）或 `human`（其余）。
    """
    return "weak" if "weak" in path.name.lower() else "human"


def chunk_corpus(docs: Sequence[SourceDoc]) -> list[ContractChunk]:
    """切片语料（复用 `SimpleChunker`；`max_chars` 与冒烟脚本一致）。

    Args:
        docs: 源文档。

    Returns:
        切片列表。
    """
    from cg_observability import TokenCounter

    chunker = SimpleChunker(counter=TokenCounter(force_estimate=True), max_chars=600)
    return [chunk for doc in docs for chunk in chunker.chunk(doc)]


def index_chunks(
    chunks: Sequence[ContractChunk], service: Any, settings: RagSettings
) -> int:
    """把切片写入向量库与稀疏库（与 async-worker 的产品入库路径一致）。

    ⚠️ 稀疏库必须一起写：否则 `RAGService._fuse()` 在稀疏路为空时直接返回稠密候选，
    `fusion` 阶段不会发生（实测：冒烟脚本此前只写向量库，fusion 恒缺失）。

    Args:
        chunks: 切片列表。
        service: 由 `build_service_from_settings()` 装配的服务。
        settings: RAG 配置（取批大小与维度）。

    Returns:
        写入向量库的条数。
    """
    texts = [c.text for c in chunks]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), settings.embed_batch_size):
        vectors.extend(
            service.embedder.embed_batch(
                texts[start : start + settings.embed_batch_size]
            )
        )
    dim = len(vectors[0]) if vectors else settings.vector_dim
    service.vector_store.ensure_collection(dim)
    records = [
        VectorRecord(
            chunk=chunk.model_copy(
                update={
                    "embedding_model": service.embedder.model_name,
                    "embedding_dim": dim,
                }
            ),
            vector=vector,
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    written = service.vector_store.upsert(records)
    if service.sparse_store is not None:
        service.sparse_store.upsert(chunks)
    return written


def dump_chunks(chunks: Sequence[ContractChunk], path: Path) -> None:
    """导出切片清单（人工标注 gold 时用它拿 `chunk_id`）。

    Args:
        chunks: 切片列表。
        path: 输出 JSONL 路径。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                "section_id": c.section_id,
                "title": c.title,
                "source_type": c.source_type.value,
                "category": c.category,
                "chars": c.char_count,
                "preview": c.text.replace("\n", " ")[:60],
            },
            ensure_ascii=False,
        )
        for c in chunks
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def make_weak_gold(chunks: Sequence[ContractChunk], path: Path) -> int:
    """自构造弱标注集：`query = section_id + 正文前 60 字`，`gold = 该切片自身`（`1-7 §6.3`）。

    Args:
        chunks: 切片列表。
        path: 输出 JSONL 路径。

    Returns:
        生成条数。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        body = chunk.text.replace("\n", " ").strip()
        # 正文通常以 section_id 开头（"第六条 逾期付款违约金 买方逾期…"），去掉重复前缀
        if body.startswith(chunk.section_id):
            body = body[len(chunk.section_id) :].lstrip()
        lines.append(
            json.dumps(
                {
                    "query_id": f"w{index:04d}",
                    "query": f"{chunk.section_id} {body[:60]}",
                    "gold_chunk_ids": [chunk.chunk_id],
                    "source": "weak",
                    "note": f"self:{chunk.doc_id}",
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return len(lines)


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------
def build_filters(item: dict[str, Any]) -> RetrievalFilters:
    """把标注行里的 `contract_type` / `source_types` 转成 `RetrievalFilters`。

    Args:
        item: 标注行。

    Returns:
        过滤条件（都缺省时为空条件）。

    Raises:
        InputError: 取值不在契约枚举内。
    """
    filters = RetrievalFilters()
    if item.get("contract_type"):
        try:
            filters.contract_type = ContractType(item["contract_type"])
        except ValueError as exc:
            raise InputError(
                f"{item['query_id']}: 非法 contract_type={item['contract_type']!r}"
            ) from exc
    if item.get("source_types"):
        try:
            filters.source_types = [SourceType(s) for s in item["source_types"]]
        except ValueError as exc:
            raise InputError(
                f"{item['query_id']}: 非法 source_types={item['source_types']!r}"
            ) from exc
    return filters


def effective_config(settings: RagSettings, args: argparse.Namespace) -> dict[str, Any]:
    """记录**实际生效**的配置（缺了它，不同配置的数字混在一起不可比，`1-7 §⑩` 避坑 1）。

    Args:
        settings: RAG 配置。
        args: 命令行参数。

    Returns:
        生效配置字典。
    """
    return {
        "vector_store": settings.vector_store,
        "qdrant_mode": settings.qdrant_mode
        if settings.vector_store == "qdrant"
        else None,
        "embed_mode": settings.embed_mode,
        "embed_base_url": settings.embed_base_url
        if settings.embed_mode == "http"
        else None,
        "embed_model": settings.embed_model if settings.embed_mode == "http" else None,
        "rerank_mode": settings.rerank_mode,
        "rerank_http_url": settings.rerank_http_url or None,
        "rerank_model": settings.rerank_model
        if settings.rerank_mode == "http"
        else None,
        "vector_dim": settings.vector_dim,
        "collection": settings.collection,
        "strategy": args.strategy or settings.strategy.value,
        "score_threshold": args.threshold
        if args.threshold is not None
        else settings.score_threshold,
        "top_k": args.top_k,
        "cutoffs": list(args.cutoffs),
    }


def _sha1_file(path: Path) -> str:
    """文件内容的短指纹（用于把"数据集变了"也纳入基线比对）。

    Args:
        path: 文件路径。

    Returns:
        12 位十六进制摘要。
    """
    return hashlib.sha1(path.read_bytes()).hexdigest()[:12]


def fingerprint(config: dict[str, Any], *, corpus: Path, gold: Path) -> str:
    """结果指纹 = **配置** + **语料内容** + **标注集内容**。

    用途：跨配置/跨数据集比对基线时拦截"拿 hash 的基线比 http 的结果"、
    或"拿弱标注集的基线比人工标注集的结果"这类不可比比较（`1-7 §⑦.4` + §⑩ 避坑 1）。
    用**内容**而不是路径，保证换目录/换相对路径不影响。

    Args:
        config: 生效配置。
        corpus: 语料文件。
        gold: 标注集文件。

    Returns:
        8 位十六进制指纹。
    """
    blob = json.dumps(config, sort_keys=True, ensure_ascii=False)
    blob += f"|corpus:{_sha1_file(corpus)}|gold:{_sha1_file(gold)}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    """跑一遍评测并返回结果字典。

    Args:
        args: 命令行参数。

    Returns:
        结果字典（`meta` / `metrics` / `per_query` / `regression`）。

    Raises:
        InputError: 输入或配置错误。
    """
    # BM25 索引是**落盘**的：用配置里的固定目录（默认 ./data/bm25）会让本次语料污染上一次的索引
    # （实测：评测语料覆盖了冒烟/单测留下的索引，融合候选串味 → 单测与演示都受影响）。
    # 评测语义上要求"每次对本语料重建"，故默认给一次性临时目录；要复用请显式 --bm25-dir。
    if args.bm25_dir is None:
        DEFAULT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        bm25_dir = Path(tempfile.mkdtemp(prefix="eval_bm25_", dir=str(DEFAULT_OUT_DIR)))
    else:
        bm25_dir = args.bm25_dir
    try:
        return _evaluate_impl(args, bm25_dir)
    finally:
        # temp dir must be removed, otherwise data/reports accumulates eval_bm25_* (912 leaked once).
        # Do not remove when --bm25-dir is given explicitly.
        if args.bm25_dir is None:
            shutil.rmtree(bm25_dir, ignore_errors=True)


def _evaluate_impl(args: argparse.Namespace, bm25_dir: Path) -> dict[str, Any]:
    """Implementation of evaluate; temp dir lifecycle is owned by the wrapper."""
    settings = RagSettings()
    settings = settings.model_copy(update={"bm25_dir": str(bm25_dir)})
    docs = load_corpus(args.corpus)
    gold = load_gold(args.gold)
    chunks = chunk_corpus(docs)
    known_ids = {c.chunk_id for c in chunks}

    missing = sorted(
        {cid for item in gold for cid in item["gold_chunk_ids"] if cid not in known_ids}
    )
    if missing:
        raise InputError(
            f"标注集里有 {len(missing)} 个 gold_chunk_id 不在语料中：{missing[:3]}"
            f"（语料重建后 chunk_id 应保持稳定；若语料已改，请用 --dump-chunks 重新标注）"
        )

    service = build_service_from_settings(settings)
    index_chunks(chunks, service, settings)

    strategy = RetrieveStrategy(args.strategy) if args.strategy else settings.strategy
    threshold = (
        args.threshold if args.threshold is not None else settings.score_threshold
    )
    max_k = max(args.cutoffs)

    per_query: list[dict[str, Any]] = []
    hits_at: dict[int, int] = {k: 0 for k in args.cutoffs}
    rr_sum = 0.0
    empty = 0
    score_sum = 0.0
    latency_sum = 0
    took_list: list[int] = []
    for item in gold:
        request = RAGQueryRequest(
            query=item["query"],
            top_k=max_k,
            # ⚠️ rerank 开启时 `_rank()` 会按 rerank_top_n 截断；不一起放大，Recall@10 恒 ≤5
            rerank_top_n=max_k,
            strategy=strategy,
            score_threshold=threshold,
            filters=build_filters(item),
        )
        response = service.retrieve(request)
        ids = [doc.chunk_id for doc in response.retrieved_docs]
        top_score = (
            response.retrieved_docs[0].scores.final if response.retrieved_docs else 0.0
        )

        rank: int | None = None
        for position, chunk_id in enumerate(ids, start=1):
            if chunk_id in set(item["gold_chunk_ids"]):
                rank = position
                break
        for cutoff in args.cutoffs:
            if rank is not None and rank <= cutoff:
                hits_at[cutoff] += 1
        reciprocal = 0.0 if rank is None else 1.0 / rank
        rr_sum += reciprocal
        if not response.retrieved_docs:
            empty += 1
        score_sum += top_score
        latency_sum += response.took_ms
        took_list.append(response.took_ms)
        per_query.append(
            {
                "query_id": item["query_id"],
                "rank_of_first_gold": rank,
                "rr": round(reciprocal, 6),
                "hits": len(ids),
                "degraded": response.degraded,
                "top_score": round(top_score, 6),
            }
        )

    total = len(gold)
    metrics: dict[str, Any] = {
        f"recall@{cutoff}": round(hits_at[cutoff] / total, 6) for cutoff in args.cutoffs
    }
    metrics["mrr"] = round(rr_sum / total, 6)
    metrics["empty_recall_rate"] = round(empty / total, 6)
    metrics["avg_top_score"] = round(score_sum / total, 6)
    metrics["empty_recall_queries"] = [
        row["query_id"] for row in per_query if row["hits"] == 0
    ]

    config = effective_config(settings, args)
    result: dict[str, Any] = {
        "meta": {
            "config": config,
            "config_fingerprint": fingerprint(
                config, corpus=args.corpus, gold=args.gold
            ),
            "corpus_file": str(args.corpus),
            "corpus_docs": len(docs),
            "corpus_chunks": len(chunks),
            "gold_file": str(args.gold),
            "gold_source": gold_source_of(args.gold),
            "gold_count": total,
            "deterministic": not args.with_timing,
        },
        "metrics": metrics,
        "per_query": per_query,
        "regression": {
            "baseline_file": None,
            "recall@5_delta": None,
            "regressed": False,
            "regressed_queries": [],
        },
    }
    if args.with_timing:
        # 耗时与时间戳都是"易变字段"：只在这里带上，保证默认输出逐字可复现
        result["meta"]["created_at"] = datetime.now(UTC).isoformat()
        metrics["avg_latency_ms"] = round(latency_sum / total, 3)
        for row, took in zip(per_query, took_list):
            row["took_ms"] = took
    return result


def apply_baseline(result: dict[str, Any], args: argparse.Namespace) -> bool:
    """与基线比对（`1-7 §7.4`）：`recall@5` 跌幅 > 0.05 → 回归。

    Args:
        result: 本次结果（原地写入 `regression`）。
        args: 命令行参数。

    Returns:
        True 表示发生回归（调用方据此返回退出码 1）。

    Raises:
        InputError: 基线文件缺失/损坏，或配置指纹不一致（除非 `--force`）。
    """
    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        raise InputError(
            f"基线文件不存在：{baseline_path}（首次建立基线请先跑一次并 --out <该路径>）"
        )
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"基线文件不是合法 JSON：{baseline_path}（{exc}）") from exc

    base_fp = baseline.get("meta", {}).get("config_fingerprint")
    if base_fp != result["meta"]["config_fingerprint"] and not args.force:
        raise InputError(
            "基线配置指纹与当前配置不一致"
            f"（baseline={base_fp} current={result['meta']['config_fingerprint']}）："
            "不同配置的数字不可比；确认要比较请加 --force"
        )

    delta = result["metrics"]["recall@5"] - baseline["metrics"]["recall@5"]
    base_ranks = {
        row["query_id"]: row["rank_of_first_gold"]
        for row in baseline.get("per_query", [])
    }
    regressed_queries = [
        row["query_id"]
        for row in result["per_query"]
        if row["rank_of_first_gold"] is None
        and base_ranks.get(row["query_id"]) is not None
    ]
    regressed = delta < -GATE_TOLERANCE
    result["regression"] = {
        "baseline_file": str(baseline_path),
        "recall@5_delta": round(delta, 6),
        "regressed": regressed,
        "regressed_queries": regressed_queries if regressed else [],
    }
    return regressed


def format_human(result: dict[str, Any]) -> str:
    """人类可读输出（卡片 §⑤ 的字段）。

    Args:
        result: 结果字典。

    Returns:
        多行文本。
    """
    meta, metrics = result["meta"], result["metrics"]
    config = meta["config"]
    lines = [
        "=" * 78,
        "检索评测（T-A8 / C10）",
        f"  config      : vector_store={config['vector_store']} embed_mode={config['embed_mode']}"
        f" rerank_mode={config['rerank_mode']}",
        f"                strategy={config['strategy']} threshold={config['score_threshold']}"
        f" top_k={config['top_k']} cutoffs={config['cutoffs']}",
        f"                embed_model={config['embed_model']} rerank_model={config['rerank_model']}",
        f"  corpus      : {meta['corpus_file']}（{meta['corpus_docs']} 篇 / {meta['corpus_chunks']} 切片）",
        f"  gold        : {meta['gold_file']}（source={meta['gold_source']}, {meta['gold_count']} 条）",
        f"  fingerprint : {meta['config_fingerprint']}",
        "-" * 78,
        "  pairs       : {n} 条 query".format(n=meta["gold_count"]),
    ]
    lines.append(
        "  "
        + "  ".join(
            f"Recall@{k}: {metrics[f'recall@{k}']:.3f}" for k in config["cutoffs"]
        )
    )
    lines.append(f"  MRR         : {metrics['mrr']:.3f}")
    latency = metrics.get("avg_latency_ms")
    latency_text = (
        f"{latency:.0f} ms/query" if latency is not None else "n/a（需 --with-timing）"
    )
    lines.append(
        f"  top_score   : {metrics['avg_top_score']:.3f}   latency: {latency_text}"
    )
    lines.append(
        f"  EMPTY_RECALL: {len(metrics['empty_recall_queries'])} 条"
        f"（率 {metrics['empty_recall_rate']:.3f}）"
        f" {metrics['empty_recall_queries'][:8]}"
    )
    regression = result["regression"]
    if regression["baseline_file"]:
        verdict = "FAIL（回归）" if regression["regressed"] else "PASS"
        lines.append(
            f"  baseline    : {regression['baseline_file']} → {verdict}"
            f"（recall@5 Δ={regression['recall@5_delta']:+.3f}，阈值 -{GATE_TOLERANCE}）"
        )
        if regression["regressed_queries"]:
            lines.append(f"  退化明细    : {regression['regressed_queries'][:10]}")
    lines.append("=" * 78)
    return "\n".join(lines)


def write_result(result: dict[str, Any], path: Path) -> None:
    """把结果落盘（JSON，UTF-8 无 BOM + LF）。

    Args:
        result: 结果字典。
        path: 输出路径。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行。

    Args:
        argv: 参数列表；空则取 `sys.argv`。

    Returns:
        解析结果。
    """
    parser = argparse.ArgumentParser(
        description="检索评测：Recall@K / MRR / 回归门禁（T-A8）"
    )
    parser.add_argument(
        "--gold", type=Path, help="标注集 JSONL（除非只用 --dump-chunks / --make-weak）"
    )
    parser.add_argument(
        "--corpus", type=Path, default=DEFAULT_CORPUS, help="语料 JSONL"
    )
    parser.add_argument(
        "--cutoffs", default="1,3,5,10", help="逗号分隔的 K（默认 1,3,5,10）"
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="每条 query 返回条数（默认=max(cutoffs)）",
    )
    parser.add_argument(
        "--strategy",
        choices=[s.value for s in RetrieveStrategy],
        default=None,
        help="检索策略；不传则取 CG_RAG_STRATEGY",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="相关性下限；不传则取 CG_RAG_SCORE_THRESHOLD（口径冻结，一般不要动）",
    )
    parser.add_argument("--out", type=Path, default=None, help="结果 JSON 落盘路径")
    parser.add_argument(
        "--baseline", type=Path, default=None, help="基线 JSON；低于基线即退出码 1"
    )
    parser.add_argument(
        "--json", action="store_true", help="只输出 JSON（便于管道/比对）"
    )
    parser.add_argument(
        "--with-timing",
        action="store_true",
        help="输出里包含时间戳与耗时（破坏逐字可复现）",
    )
    parser.add_argument(
        "--dump-chunks",
        type=Path,
        default=None,
        help="导出切片清单后退出（人工标注用）",
    )
    parser.add_argument(
        "--make-weak", type=Path, default=None, help="生成弱标注集后退出"
    )
    parser.add_argument(
        "--bm25-dir",
        type=Path,
        default=None,
        help="BM25 索引目录；默认用**一次性临时目录**（不同语料共用一个目录会互相污染）",
    )
    parser.add_argument("--force", action="store_true", help="忽略基线配置指纹不一致")
    args = parser.parse_args(argv)
    args.cutoffs = tuple(int(x) for x in str(args.cutoffs).split(",") if x.strip())
    if not args.cutoffs:
        raise InputError("--cutoffs 不能为空")
    if args.top_k is None:
        args.top_k = max(args.cutoffs)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """主流程。

    Args:
        argv: 参数列表。

    Returns:
        退出码：0 通过 / 1 回归退化 / 2 输入或配置错误。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    try:
        args = parse_args(argv)
        if args.json:
            silence_logs()
        docs = load_corpus(args.corpus)
        if args.dump_chunks:
            chunks = chunk_corpus(docs)
            dump_chunks(chunks, args.dump_chunks)
            print(f"切片清单已写出：{args.dump_chunks}（{len(chunks)} 条）")
            return 0
        if args.make_weak:
            count = make_weak_gold(chunk_corpus(docs), args.make_weak)
            print(f"弱标注集已写出：{args.make_weak}（{count} 条）")
            return 0
        if args.gold is None:
            raise InputError("缺少 --gold（或用 --dump-chunks / --make-weak）")

        started = time.perf_counter()
        result = evaluate(args)
        regressed = apply_baseline(result, args) if args.baseline else False

        out_path = args.out
        if out_path is None:
            stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            out_path = DEFAULT_OUT_DIR / f"eval_{stamp}.json"
        write_result(result, out_path)

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_human(result))
            print(f"  结果已落盘：{out_path}")
            print(f"  总耗时：{round(time.perf_counter() - started, 2)}s")
        return 1 if regressed else 0
    except InputError as exc:
        print(f"[输入/配置错误] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
