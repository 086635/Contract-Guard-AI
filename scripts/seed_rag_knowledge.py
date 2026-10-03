#!/usr/bin/env python
"""T-D8-1 / C76：把赛题语料灌入 RAG 知识库（**幂等**）。

做两件事：
1. `STANDARD_CLAUSE` → 写入 PG `standard_clauses`（人工维护源；`source=eval_corpus:<doc_id>` 作幂等键）
   **并**把语料切片写入向量库/稀疏库；
2. `LAW_ARTICLE` → 只写向量库/稀疏库（`standard_clauses` 表的 `contract_type` CHECK 仅支持 6 种合同类型、
   无法条列，硬塞会被 DB 拒绝）。

🔴 **复用既有唯一实现**：切片与写入直接调用 `scripts/eval_recall.py` 的
`load_corpus` / `chunk_corpus` / `index_chunks` —— 这样灌进向量库的 `chunk_id` 与
`scripts/data/gold.jsonl` 标注**天然一致**，`eval_recall.py` 才能量到召回。
不新增第二套切片或写入逻辑。

🔴 **不改代码默认值**：向量库/embedding/rerank 一律沿用环境变量（`CG_RAG_*`）；
本脚本不写任何默认值，也不打印密钥。

用法::

    python scripts/seed_rag_knowledge.py --dry-run
    python scripts/seed_rag_knowledge.py
    python scripts/seed_rag_knowledge.py --corpus scripts/data/eval_corpus.jsonl

退出码：0 = 成功；2 = 参数/环境/依赖错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 让脚本直接从仓库根目录运行：把 workspace 内的包加入 sys.path（无需 pip install）
# ---------------------------------------------------------------------------
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
    _REPO_ROOT / "scripts",
):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import eval_recall  # noqa: E402  # 复用唯一切片/入库实现

from app.db.repository.standard_clause import (  # noqa: E402
    StandardClauseCreate,
    StandardClauseRepository,
)
from app.db.session import SessionLocal  # noqa: E402
from app.generated.schemas import ContractType as ApiContractType  # noqa: E402
from rag_core.config import RagSettings  # noqa: E402
from rag_core.factory import build_service_from_settings  # noqa: E402

DEFAULT_CORPUS = _REPO_ROOT / "scripts" / "data" / "eval_corpus.jsonl"
SOURCE_PREFIX = "eval_corpus:"


def _title_of(text: str) -> str:
    """取正文首个非空行作标题（截断到 200 字符，满足表结构上限）。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:200]
    return "未命名条款"


def _standard_docs(docs):
    return [d for d in docs if getattr(d, "source_type", "") == "STANDARD_CLAUSE"]


async def _seed_standard_clauses(docs) -> dict:
    """把标准条款写入 PG（按 `source` 幂等），返回计数。"""
    created = 0
    skipped = 0
    async with SessionLocal() as session:
        repo = StandardClauseRepository(session)
        existing = {record.source for record in await repo.list_all()}
        for doc in docs:
            source = f"{SOURCE_PREFIX}{doc.doc_id}"
            if source in existing:
                skipped += 1
                continue
            await repo.create(
                StandardClauseCreate(
                    title=_title_of(doc.text),
                    contract_type=ApiContractType(doc.contract_type),
                    text=doc.text,
                    source=source,
                )
            )
            created += 1
        await session.commit()
    return {"created": created, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-D8-1：RAG 知识库灌数（幂等）")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--dry-run", action="store_true", help="只解析与统计，不写 PG / 向量库"
    )
    args = parser.parse_args(argv)

    if not args.corpus.is_file():
        print(f"语料不存在：{args.corpus}", file=sys.stderr)
        return 2

    try:
        docs = eval_recall.load_corpus(args.corpus)
    except Exception as exc:  # noqa: BLE001 - CLI 兜底
        print(f"语料加载失败：{type(exc).__name__}", file=sys.stderr)
        return 2

    standard = _standard_docs(docs)
    laws = [d for d in docs if d not in standard]
    chunks = eval_recall.chunk_corpus(docs)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dryRun": True,
                    "corpus": str(args.corpus),
                    "docs": len(docs),
                    "standardClauses": len(standard),
                    "lawArticles": len(laws),
                    "chunks": len(chunks),
                },
                ensure_ascii=False,
            )
        )
        return 0

    settings = RagSettings()
    try:
        service = build_service_from_settings(settings)
        indexed = eval_recall.index_chunks(chunks, service, settings)
    except Exception as exc:  # noqa: BLE001 - 外部依赖不可用必须显式失败
        print(f"索引写入失败：{type(exc).__name__}", file=sys.stderr)
        return 2

    try:
        pg = asyncio.run(_seed_standard_clauses(standard))
    except Exception as exc:  # noqa: BLE001
        print(f"标准条款落库失败：{type(exc).__name__}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "corpus": str(args.corpus),
                "docs": len(docs),
                "standardClauses": len(standard),
                "lawArticles": len(laws),
                "chunks": len(chunks),
                "indexed": indexed,
                "pgCreated": pg["created"],
                "pgSkipped": pg["skipped"],
                "vectorStore": type(service.vector_store).__name__,
                "embedder": type(service.embedder).__name__,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
