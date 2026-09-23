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

一条命令证明「hybrid + 真实精排」生效（T-A6 验收口径，见 `1-6语义服务接入.md` §⑦.1）：

    # ① 起本地 rerank 服务（Docker 不可用时用这个；TEI 容器是生产形态）
    python scripts/serve_rerank.py                       # 默认 127.0.0.1:8081，模型走本地 HF 缓存
    # ② 混合检索 + 真实精排 + Qdrant 本地内存模式
    $env:CG_RAG_VECTOR_STORE="qdrant"; $env:CG_RAG_QDRANT_MODE="local_memory"
    $env:CG_RAG_STRATEGY="HYBRID_RRF"
    $env:CG_RAG_RERANK_MODE="http"; $env:CG_RAG_RERANK_HTTP_URL="http://127.0.0.1:8081"
    python scripts/phase1_smoke.py

    # 判定：每条查询的 [证据] 行里
    #   · stages 含 sparse 与 fusion（=稀疏路有命中、融合真的在跑）
    #   · rerank_model ≠ none/noop（=精排真的算分了，而不是恒等透传）
    # ⚠️ 注意：当前编排器对 DENSE_ONLY **也会**跑稀疏+融合（strategy 只影响日志与
    #    "无稀疏库时强制降级"），所以这两个阶段**不能**单独用来证明 hybrid 生效 ——
    #    真正的判据是上面的 rerank_model，以及稀疏路确实有命中（见 `1-7` 的落地记录）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
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

from cg_observability import (  # noqa: E402
    TokenCounter,
    TraceContext,
    get_logger,
    log_event,
)
from rag_contracts.enums import RetrieveStrategy  # noqa: E402
from rag_contracts.errors import RagError  # noqa: E402
from rag_contracts.query import RAGQueryRequest  # noqa: E402
from rag_core.chunking import SimpleChunker, SourceDoc  # noqa: E402
from rag_core.config import RagSettings  # noqa: E402
from rag_core.factory import (  # noqa: E402
    _resolve_embed_endpoint,
    build_service_from_settings,
)

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
    sparse_store: Any = None,
) -> int:
    """向量化 + 入库阶段（**向量库与稀疏库都要写**）：打 embed.done / upsert.done 埋点。

    Args:
        chunks: 切片列表。
        embedder: 向量化实现。
        store: 向量库实现。
        counter: Token 计数器。
        batch_size: 批大小。
        sparse_store: 稀疏（BM25）实现；非 None 时同步写入，否则 hybrid 会因稀疏路为空
            而**跳过 fusion**（产品链路的入库由 async-worker 负责，本脚本要与之一致）。

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

    if sparse_store is not None:
        started = time.perf_counter()
        sparse_written = sparse_store.upsert(chunks)
        log_event(
            _LOG,
            "upsert.done",
            store=getattr(sparse_store, "name", type(sparse_store).__name__),
            count=sparse_written,
            failed_count=len(chunks) - sparse_written,
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


def _embed_failure_hint(settings: RagSettings) -> str:
    """向量化失败时给出可照做的排查提示（针对本仓最常见的两个坑）。

    Args:
        settings: 当前 RAG 配置。

    Returns:
        多行提示文本。
    """
    lines = ["      排查："]
    if settings.embed_mode == "http":
        # 用 `_resolve_embed_endpoint`（私有名）取**解析后的真实端点**：
        # 它是唯一真相来源 —— 直接打原始配置会把 `CG_RAG_EMBED_BASE_URL=ollama`
        # 显示成 `base_url='ollama'`（path/model 还是 TEI 的默认值），把排查方向带偏。
        resolved = _resolve_embed_endpoint(settings)
        lines.append(
            f"      1) 解析后的真实端点："
            f"{(resolved['base_url'] + resolved['path'])!r}  "
            f"model={resolved['model']!r}（provider={resolved['provider'] or '自定义'}）；"
            "服务未起 / 路径不对都会走到这里。"
        )
        lines.append(
            f"         原始配置：base_url={settings.embed_base_url!r}  "
            f"path={settings.embed_path!r}  model={settings.embed_model!r}"
        )
        lines.append(
            "      2) 注意 embed_base_url 的**代码默认值是 TEI 的 :8080**，不是 ollama。"
            "接 ollama 必须显式设 CG_RAG_EMBED_BASE_URL=ollama"
            "（等价 http://localhost:11434/v1 + /embeddings + bge-m3）。"
        )
        lines.append(
            "      3) 首次调用含模型冷启动（本机 bge-m3 实测 34~41s），"
            f"而 CG_RAG_EMBED_TIMEOUT_MS 默认 3000 → 必须调大（如 120000）。"
            f"当前值：{settings.embed_timeout_ms}。"
        )
    else:
        lines.append("      当前 embed_mode 走离线路径，出现该错误说明是本地实现问题。")
    return "\n".join(lines)


def main() -> int:
    """主流程。

    Returns:
        进程退出码（0 成功）。
    """
    parser = argparse.ArgumentParser(description="Phase 1 RAG 全链路冒烟")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="JSONL 语料路径")
    parser.add_argument("--top-k", type=int, default=3, help="每条查询返回条数")
    parser.add_argument(
        "--strategy",
        choices=[s.value for s in RetrieveStrategy],
        default=None,
        help=(
            "检索策略；不传则取 CG_RAG_STRATEGY（未设时即 DENSE_ONLY）。"
            "HYBRID_RRF 才有融合路；精排是否生效与此无关（只看装配了哪个重排器）"
        ),
    )
    args = parser.parse_args()

    # 本机控制台默认 GBK：把输出重定向到文件时，任何 ⚠️ 之类字符都会让脚本崩在 print 上
    # （实测 UnicodeEncodeError: 'gbk' codec）。评测/证据都靠重定向采集，故显式切 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

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
    # 策略优先级：命令行 > CG_RAG_STRATEGY。既有行为（未指定时）仍是 DENSE_ONLY。
    strategy = RetrieveStrategy(args.strategy) if args.strategy else settings.strategy

    print("=" * 78)
    print("Phase 1 冒烟：切片 → 向量化 → 入库 → 检索")
    print(f"  trace_id：{trace_id}")
    print(
        f"  配置：store={settings.vector_store}  embed={settings.embed_mode}  "
        f"rerank={settings.rerank_mode}  dim={settings.vector_dim}  策略={strategy.value}"
    )
    print("=" * 78)

    # ---- 组件装配：**走生产入口**（与 api / worker 同一套工厂） ----
    # 只手工建 chunker（切片不属于 RAGService）；embedder / vector_store / 稀疏路 / reranker，
    # 以及服务级默认值（strategy / score_threshold / rerank_top_n / rerank_threshold）全部来自工厂。
    # ⚠️ 手工拼 RAGService 会漏掉稀疏路与 rerank_threshold —— HYBRID_RRF 会静默退化成 dense-only、
    #    精排阈值也不生效（T-A6 实测缺陷）；这是"一条命令证明 hybrid / rerank 生效"的前提。
    service = build_service_from_settings(settings)
    counter = service.counter
    chunker = SimpleChunker(counter=counter, max_chars=600)
    embedder = service.embedder
    store = service.vector_store
    reranker = service.reranker
    rerank_model = (
        getattr(reranker, "model_name", "none") if reranker is not None else "none"
    )
    # 精排是否真的会算分：只看 request.enable_rerank 与「是否装配了重排器」，
    # 与检索策略无关。noop 是恒等透传（final 分仍等于余弦分），不算真实精排。
    if reranker is None or settings.rerank_mode == "noop":
        print(
            "  [提示] 精排未生效（rerank=noop 恒等透传 / rerank_enabled=False）→ 得分仍是余弦口径。\n"
            "         要验证精排：CG_RAG_RERANK_MODE=http + 一个真实可用的 /rerank 服务。\n"
            "         ⚠️ ollama 没有 rerank 端点（实测 /rerank、/v1/rerank 均 404）；\n"
            "         本地可直接起 scripts/serve_rerank.py（见该文件头部用法）。"
        )
    print(
        f"  [装配] embed={type(embedder).__name__}"
        f"  store={getattr(store, 'name', type(store).__name__)}"
        f"  sparse={'on' if service.sparse_store is not None else 'off'}"
        f"  rerank={rerank_model}"
    )

    # ---- ① 切片 ----
    docs = load_corpus(args.corpus)
    print(f"\n[1/4] 切片：载入 {len(docs)} 篇源文档 → {args.corpus.name}")
    chunks, chunk_tokens = run_chunking(docs, chunker)
    print(f"      产出 {len(chunks)} 个切片，Token 合计 {chunk_tokens}")

    # ---- ② 向量化 + ③ 入库 ----
    print(f"\n[2/4] 向量化：model={embedder.model_name} dim={embedder.dim}")
    print("[3/4] 入库")
    # ⚠️ 建库阶段**没有降级**（只有 retrieve 有）：外部语义服务不可用时会直接抛错。
    # 这里把它收敛成一句可读结论，避免用户被裸 traceback 误导为"代码坏了"。
    try:
        written = run_indexing(
            chunks,
            embedder,
            store,
            counter,
            settings.embed_batch_size,
            sparse_store=service.sparse_store,
        )
    except RagError as exc:
        print(f"      [FAIL] 向量化/入库失败：{exc}")
        print(_embed_failure_hint(settings))
        return 2
    sparse_note = (
        f"（稀疏路 {'on' if service.sparse_store is not None else 'off'}）"
        if service.sparse_store is not None
        else "（稀疏路 off）"
    )
    print(f"      写入 {written} 条，库内共 {store.count()} 条 {sparse_note}")
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
            strategy=strategy,
            score_threshold=settings.score_threshold,
            max_context_tokens=settings.max_context_tokens,
        )
        response = service.retrieve(request)
        total_tokens += response.token_usage.total_tokens
        if not response.retrieved_docs:
            empty_count += 1
        print_table(response)
        print(
            f"  [证据] strategy={response.strategy.value}  rerank_model={rerank_model}"
            f"  stages={'+'.join(sorted(response.stage_latency_ms)) or '-'}"
        )

    # ---- 汇总 ----
    print("\n" + "=" * 78)
    print("汇总")
    print(f"  切片数：{len(chunks)}")
    print(f"  入库数：{written}")
    print(f"  查询数：{len(DEMO_QUERIES)}（空召回 {empty_count} 条）")
    print(f"  策略：{strategy.value}（实际生效值见各条 [证据] 行）")
    print(f"  精排模型：{rerank_model}")
    print(f"  Token 耗用（检索阶段合计）：{total_tokens}")
    print(f"  Token 口径：{'估算(len/1.5)' if counter.estimated else 'tiktoken 精确'}")
    print("=" * 78)
    if empty_count and settings.embed_mode == "hash":
        print(
            f"\n说明：有 {empty_count} 条查询走的是 EMPTY_RECALL 降级 —— 这是**设计行为**而非故障。\n"
            f"      当前向量化用的是 HashEmbedder（离线哈希伪向量，语义表达力有限），\n"
            f"      部分查询的 top1 余弦分低于阈值 {settings.score_threshold} 便判为【缺失标准对照】。\n"
            f"      接入真实模型后同一条查询会正常召回（两种端点任选其一）：\n"
            f"        # A) 本机 ollama（已下 bge-m3 时最省事）——注意冷启动需调大超时\n"
            f"        $env:CG_RAG_EMBED_MODE='http'; $env:CG_RAG_EMBED_BASE_URL='ollama'\n"
            f"        $env:CG_RAG_EMBED_TIMEOUT_MS='120000'; python scripts/phase1_smoke.py\n"
            f"        # B) TEI（端口 8080，路径 /embed）\n"
            f"        $env:CG_RAG_EMBED_MODE='http'; $env:CG_RAG_EMBED_BASE_URL='tei'\n"
            f"        $env:CG_RAG_EMBED_MODEL='BAAI/bge-m3'; python scripts/phase1_smoke.py"
        )
    elif empty_count:
        print(
            f"\n说明：有 {empty_count} 条查询仍为 EMPTY_RECALL。向量化已是真实模型"
            f"（embed_mode={settings.embed_mode}），请检查：\n"
            f"      · score_threshold={settings.score_threshold} 是否偏高；\n"
            f"      · 精排是否生效（rerank={settings.rerank_mode}，noop 为恒等透传）。"
        )
    print("\n提示：日志为单行 JSON，可用 `... | python -c \"import sys,json;print(json.load(sys.stdin)['stage'])\"` 解析。")
    print("下一步：CG_RAG_VECTOR_STORE=qdrant 可切换到真实 Qdrant（本地内存模式）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
