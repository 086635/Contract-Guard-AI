#!/usr/bin/env python
"""Phase 1 全链路冒烟：切片 → 向量化 → 入库 → 检索 → 打印。

一条命令跑通（**离线即可**，不需要 API Key / 外网 / GPU）：

    python scripts/phase1_smoke.py

切换向量库 / 向量化方式（同一套代码，只改环境变量）：

    CG_RAG_VECTOR_STORE=qdrant python scripts/phase1_smoke.py
    CG_RAG_EMBED_MODE=http CG_RAG_EMBED_BASE_URL=http://localhost:8080 python scripts/phase1_smoke.py

验收口径（设计文档 T02）：
1. 退出码 0；
2. 日志出现 chunk.done / embed.done / upsert.done / retrieve.done 四条；
3. 关键 Trace 字段：avg_chars（切片平均长度）、token_count（Token 耗用）、top_score（检索得分）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# 让脚本直接从仓库根目录运行：把 workspace 内的包加入 sys.path（无需 pip install）
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _pkg in ("rag-contracts", "cg-observability", "rag-core"):
    _path = _REPO_ROOT / "packages" / _pkg
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cg_observability import (  # noqa: E402
    TokenCounter,
    TraceContext,
    get_logger,
    log_event,
)
from rag_contracts.enums import RetrieveStrategy  # noqa: E402
from rag_contracts.query import RAGQueryRequest  # noqa: E402
from rag_core.chunking import SimpleChunker, SourceDoc  # noqa: E402
from rag_core.config import RagSettings  # noqa: E402
from rag_core.factory import (  # noqa: E402
    build_embedder,
    build_reranker,
    build_vector_store,
)
from rag_core.service import RAGService  # noqa: E402

_LOG = get_logger("scripts.phase1_smoke")

DEFAULT_CORPUS = _REPO_ROOT / "scripts" / "data" / "phase1_sample.jsonl"

# 演示用查询：覆盖"标准条款比对"与"法律依据检索"两类意图
DEMO_QUERIES: tuple[str, ...] = (
    "买方逾期付款的违约金怎么约定，上限是多少",
    "试用期最长可以约定多久",
    "不可抗力导致合同无法履行，责任如何免除",
)


def load_corpus(path: Path) -> list[SourceDoc]:
    """从 JSONL 语料文件加载源文档。

    Args:
        path: JSONL 路径，每行一个源文档。

    Returns:
        SourceDoc 列表。

    Raises:
        FileNotFoundError: 语料文件不存在。
    """
    if not path.exists():
        raise FileNotFoundError(f"语料文件不存在：{path}")
    docs: list[SourceDoc] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(SourceDoc.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            _LOG.warning("语料第 %d 行解析失败，已跳过：%s", line_no, exc)
    return docs


def run_chunking(docs: Sequence[SourceDoc], chunker: SimpleChunker) -> tuple[list[Any], int]:
    """切片阶段：逐文档切分并打 chunk.start / chunk.done 埋点。

    Args:
        docs: 源文档序列。
        chunker: 切片器。

    Returns:
        (全部切片, Token 总数)。
    """
    all_chunks: list[Any] = []
    total_tokens = 0
    for doc in docs:
        log_event(
            _LOG,
            "chunk.start",
            doc_id=doc.doc_id,
            source_type=doc.source_type.value,
            raw_chars=len(doc.text),
        )
        chunks, stats = chunker.chunk_with_stats(doc)
        all_chunks.extend(chunks)
        total_tokens += stats.total_tokens
        log_event(
            _LOG,
            "chunk.done",
            doc_id=doc.doc_id,
            source_type=doc.source_type.value,
            **stats.to_log_fields(),
        )
    return all_chunks, total_tokens


def run_indexing(
    chunks: Sequence[Any],
    embedder: Any,
    store: Any,
    counter: TokenCounter,
    batch_size: int,
) -> int:
    """向量化 + 入库阶段：打 embed.done / upsert.done 埋点。

    Args:
        chunks: 切片列表。
        embedder: 向量化实现。
        store: 向量库实现。
        counter: Token 计数器。
        batch_size: 批大小。

    Returns:
        成功入库条数。
    """
    texts = [c.text for c in chunks]
    started = time.perf_counter()
    vectors: list[list[float]] = []
    batch_count = 0
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        vectors.extend(embedder.embed_batch(batch))
        batch_count += 1
    embed_ms = int((time.perf_counter() - started) * 1000)
    embed_tokens = counter.count_many(texts)
    log_event(
        _LOG,
        "embed.done",
        model=getattr(embedder, "model_name", "unknown"),
        batch_count=batch_count,
        batch_size=batch_size,
        dim=len(vectors[0]) if vectors else 0,
        embed_tokens=embed_tokens,
        token_count=embed_tokens,
        latency_ms=embed_ms,
    )

    dim = len(vectors[0]) if vectors else 0
    store.ensure_collection(dim)

    from rag_contracts.ports import VectorRecord

    started = time.perf_counter()
    records = [
        VectorRecord(
            chunk=chunk.model_copy(
                update={"embedding_model": embedder.model_name, "embedding_dim": dim}
            ),
            vector=vector,
        )
        for chunk, vector in zip(chunks, vectors)
    ]
    written = store.upsert(records)
    log_event(
        _LOG,
        "upsert.done",
        store=getattr(store, "name", type(store).__name__),
        count=written,
        failed_count=len(records) - written,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return written


def print_table(response: Any, *, max_text: int = 46) -> None:
    """把检索结果以表格形式打印到控制台。

    Args:
        response: RAGQueryResponse。
        max_text: 正文预览最大字符数。
    """
    print(f"\n  查询：{response.query}")
    print(f"  trace_id={response.trace_id}  策略={response.strategy.value}  "
          f"候选={response.total_candidates}  返回={len(response.retrieved_docs)}  "
          f"耗时={response.took_ms}ms")
    if not response.retrieved_docs:
        print(f"  [EMPTY] {response.fallback_reason.value} —— {response.context_block}")
        return
    header = f"  {'#':<3}{'score':<8}{'source':<17}{'section':<14}{'title'}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for doc in response.retrieved_docs:
        title = (doc.title or doc.citation.sourceTitle)[:16]
        print(
            f"  {doc.scores.rank:<3}{doc.scores.final:<8.4f}"
            f"{doc.source_type.value:<17}{doc.section_id:<14}{title}"
        )
        preview = doc.text[:max_text].replace("\n", " ")
        print(f"      └─ {preview}{'…' if len(doc.text) > max_text else ''}")


def main() -> int:
    """主流程。

    Returns:
        进程退出码（0 成功）。
    """
    parser = argparse.ArgumentParser(description="Phase 1 RAG 全链路冒烟")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="JSONL 语料路径")
    parser.add_argument("--top-k", type=int, default=3, help="每条查询返回条数")
    args = parser.parse_args()

    settings = RagSettings()

    # 全链路共用同一个 trace_id：从切片到检索的全部日志可用它一次拉齐
    with TraceContext(name="phase1_smoke") as ctx:
        return _run(settings=settings, args=args, trace_id=ctx.trace_id)


def _run(*, settings: RagSettings, args: argparse.Namespace, trace_id: str) -> int:
    """在已绑定的 trace 上下文中执行完整流程。

    Args:
        settings: RAG 配置。
        args: 命令行参数。
        trace_id: 全链路追踪 ID。

    Returns:
        进程退出码（0 成功）。
    """
    print("=" * 78)
    print("Phase 1 冒烟：切片 → 向量化 → 入库 → 检索")
    print(f"  trace_id：{trace_id}")
    print(
        f"  配置：store={settings.vector_store}  embed={settings.embed_mode}  "
        f"rerank={settings.rerank_mode}  dim={settings.vector_dim}  策略={settings.strategy.value}"
    )
    print("=" * 78)

    # ---- 组件装配（与生产同一套工厂，只是默认值不同） ----
    counter = TokenCounter()
    chunker = SimpleChunker(counter=counter, max_chars=600)
    embedder = build_embedder(settings)
    store = build_vector_store(settings)
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=counter,
        reranker=build_reranker(settings),
        dense_top=settings.dense_top,
        default_max_context_tokens=settings.max_context_tokens,
    )

    # ---- ① 切片 ----
    docs = load_corpus(args.corpus)
    print(f"\n[1/4] 切片：载入 {len(docs)} 篇源文档 → {args.corpus.name}")
    chunks, chunk_tokens = run_chunking(docs, chunker)
    print(f"      产出 {len(chunks)} 个切片，Token 合计 {chunk_tokens}")

    # ---- ② 向量化 + ③ 入库 ----
    print(f"\n[2/4] 向量化：model={embedder.model_name} dim={embedder.dim}")
    print("[3/4] 入库")
    written = run_indexing(chunks, embedder, store, counter, settings.embed_batch_size)
    print(f"      写入 {written} 条，库内共 {store.count()} 条")
    if written == 0:
        print("      [FAIL] 没有写入任何向量，链路中断")
        return 1

    # ---- ④ 检索 ----
    print(f"\n[4/4] 检索：{len(DEMO_QUERIES)} 条演示查询")
    total_tokens = 0
    empty_count = 0
    for query in DEMO_QUERIES:
        request = RAGQueryRequest(
            trace_id=trace_id,  # 与建库阶段共用同一 trace，便于全链路排查
            query=query,
            top_k=args.top_k,
            strategy=RetrieveStrategy.DENSE_ONLY,
            score_threshold=settings.score_threshold,
            max_context_tokens=settings.max_context_tokens,
        )
        response = service.retrieve(request)
        total_tokens += response.token_usage.total_tokens
        if not response.retrieved_docs:
            empty_count += 1
        print_table(response)

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("汇总")
    print(f"  切片数：{len(chunks)}")
    print(f"  入库数：{written}")
    print(f"  查询数：{len(DEMO_QUERIES)}（空召回 {empty_count} 条）")
    print(f"  Token 耗用（检索阶段合计）：{total_tokens}")
    print(f"  Token 口径：{'估算(len/1.5)' if counter.estimated else 'tiktoken 精确'}")
    print("=" * 78)
    if empty_count:
        print(
            f"\n说明：有 {empty_count} 条查询走的是 EMPTY_RECALL 降级 —— 这是**设计行为**而非故障。\n"
            f"      当前向量化用的是 HashEmbedder（离线哈希伪向量，语义表达力有限），\n"
            f"      部分查询的 top1 余弦分低于阈值 {settings.score_threshold} 便判为【缺失标准对照】。\n"
            f"      接入真实模型后同一条查询会正常召回：\n"
            f"        CG_RAG_EMBED_MODE=http CG_RAG_EMBED_BASE_URL=http://localhost:8080 \\\n"
            f"        CG_RAG_EMBED_MODEL=BAAI/bge-m3 python scripts/phase1_smoke.py"
        )
    print("\n提示：日志为单行 JSON，可用 `... | python -c \"import sys,json;print(json.load(sys.stdin)['stage'])\"` 解析。")
    print("下一步：CG_RAG_VECTOR_STORE=qdrant 可切换到真实 Qdrant（本地内存模式）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
