"""官方三库资料导入：校验来源/原文，复用原切片、条款仓储和 RAG 索引。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parents[1]
for _path in (
    _ROOT / "scripts",
    *(
        _ROOT / "packages" / name
        for name in (
            "rag-contracts",
            "rag-core",
            "cg-observability",
            "ai-engine",
            "doc-processor",
        )
    ),
    _ROOT / "apps/api-gateway",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import eval_recall  # noqa: E402

from app.db.repository.standard_clause import (  # noqa: E402
    StandardClauseCreate,
    StandardClauseRepository,
)
from app.db.session import SessionLocal  # noqa: E402
from app.generated.schemas import ContractType as ApiContractType  # noqa: E402
from app.services.standard_clause_service import StandardClauseIndexer  # noqa: E402
from rag_contracts.enums import SourceType  # noqa: E402
from rag_contracts.patterns import CLAUSE_HEAD_RE  # noqa: E402
from rag_core.chunking import SourceDoc  # noqa: E402
from rag_core.config import RagSettings  # noqa: E402
from rag_core.factory import build_service_from_settings  # noqa: E402


def normalize_law_text(text: str) -> str:
    """网页法条合并在一行时恢复段落；编号判定复用契约层原正则。"""
    lines: list[str] = []
    current: list[str] = []
    for word in text.split():
        match = CLAUSE_HEAD_RE.match(word)
        if match and match.group("num").endswith("条") and not match.group("rest"):
            if current:
                lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def load_sources(manifest_path: Path) -> tuple[dict, list[SourceDoc]]:
    """严格验证官方来源和本机原文哈希；不执行清单中的代码或远程下载。"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("documents")
    if not isinstance(entries, list) or not entries:
        raise ValueError("来源清单为空")
    docs: list[SourceDoc] = []
    seen: set[str] = set()
    for entry in entries:
        host = urlsplit(entry["source_url"]).hostname or ""
        if (
            not host.endswith(".gov.cn")
            or urlsplit(entry["source_url"]).scheme != "https"
        ):
            raise ValueError("来源必须是官方 HTTPS 地址")
        doc_id = entry["doc_id"]
        if doc_id in seen:
            raise ValueError("来源标识重复")
        seen.add(doc_id)
        path = (manifest_path.parent / entry["file"]).resolve()
        if not path.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("原文必须位于清单目录")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("原文哈希不匹配")
        text = raw.decode("utf-8").strip()
        if not text or not entry.get("title") or not entry.get("scope"):
            raise ValueError("来源缺少正文、标题或适用范围")
        if entry["library"] == "law":
            normalized = normalize_law_text(text)
            docs.append(
                SourceDoc.from_dict(
                    {
                        "doc_id": doc_id,
                        "source_type": SourceType.LAW_ARTICLE.value,
                        "text": normalized,
                        "law_code": entry["code"],
                        "law_title": entry["title"],
                        "effective_date": entry["effective_date"],
                        "category": "OFFICIAL_LAW",
                        "tags": [entry["source_url"]],
                    }
                )
            )
        elif entry["library"] == "standard":
            # 使用说明不是合同条款，正文从原编号规则识别的第一条开始。
            rows = text.splitlines()
            start = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if (match := CLAUSE_HEAD_RE.match(row))
                    and match.group("num").endswith("条")
                ),
                None,
            )
            if start is None:
                raise ValueError("范本没有合同条款正文")
            paragraphs: list[str] = []
            for row in rows[start:]:
                match = CLAUSE_HEAD_RE.match(row)
                if match and match.group("num").endswith("条"):
                    paragraphs.append(row.strip())
                elif paragraphs and row.strip():
                    paragraphs[-1] += " " + row.strip()
            text = "\n".join(paragraphs)
            for contract_type in entry["contract_types"]:
                docs.append(
                    SourceDoc.from_dict(
                        {
                            "doc_id": f"{doc_id}:{contract_type}",
                            "source_type": SourceType.STANDARD_CLAUSE.value,
                            "text": text,
                            "contract_type": contract_type,
                            "category": "OFFICIAL_TEMPLATE",
                            "tags": [entry["source_url"]],
                        }
                    )
                )
        else:
            raise ValueError("未知资料类型")
    return manifest, docs


async def seed_clauses(chunks: list, manifest: dict, service) -> dict:
    """官方范本条款写入原人工维护表；沿用 std:<uuid> 索引，编辑/停用即时生效。"""
    sources = {entry["doc_id"]: entry for entry in manifest["documents"]}
    created = 0
    skipped = 0
    records = []
    async with SessionLocal() as session:
        repo = StandardClauseRepository(session)
        existing = {record.source: record for record in await repo.list_all()}
        for chunk in chunks:
            if chunk.source_type != SourceType.STANDARD_CLAUSE:
                continue
            entry = sources[chunk.doc_id.rsplit(":", 1)[0]]
            source = f"official:{chunk.chunk_id}"
            record = existing.get(source)
            if record is None:
                scope = (
                    f"【官方参考范本；适用范围：{entry['scope']}；不等于企业审批基线】"
                )
                record = await repo.create(
                    StandardClauseCreate(
                        title=f"官方范本 {entry['code']} · {chunk.section_id}"[:200],
                        contract_type=ApiContractType(chunk.contract_type.value),
                        text=f"{scope}\n{chunk.text}\n来源：{entry['source_url']}",
                        source=source,
                    )
                )
                created += 1
            else:
                skipped += 1
            if record.is_enabled:
                records.append(record)
        indexed = StandardClauseIndexer(service.embedder, service.vector_store).upsert(
            records
        )
        await session.commit()
    return {"created": created, "existing": skipped, "indexed": indexed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="官方标准/条款/法典初始库导入")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest, docs = load_sources(args.manifest)
        chunks = [
            chunk
            for chunk in eval_recall.chunk_corpus(docs)
            if chunk.section_id != "PREAMBLE"
        ]
        laws = [
            chunk for chunk in chunks if chunk.source_type == SourceType.LAW_ARTICLE
        ]
        for chunk in laws:
            chunk.title = f"{chunk.law_title} {chunk.section_id}"
        summary = {
            "sourceDocuments": len(manifest["documents"]),
            "chunks": len(chunks),
            "lawChunks": len(laws),
            "standardChunks": len(chunks) - len(laws),
        }
        if not args.dry_run:
            settings = RagSettings()
            if settings.vector_store != "qdrant" or settings.qdrant_mode != "server":
                raise ValueError("应用多进程导入需要显式配置 Qdrant server")
            if settings.embed_mode != "http":
                raise ValueError("官方库需要真实语义向量")
            service = build_service_from_settings(settings)
            summary["lawIndexed"] = eval_recall.index_chunks(laws, service, settings)
            summary["clauses"] = asyncio.run(seed_clauses(chunks, manifest, service))
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 返回安全错误类别，不回显凭据
        print(f"官方资料导入失败：{type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
