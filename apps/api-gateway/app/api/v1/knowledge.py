"""已登录管理员的三库来源、语义诊断和只读检索预览。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ai_engine.agents.tools import build_rag_tool
from ai_engine.llm.settings import get_llm_settings
from app.api.deps import get_service_context, require_roles
from app.db.repository.standard_clause import StandardClauseRepository
from app.generated.schemas import (
    KnowledgeSearchResponse,
    KnowledgeSourcesResponse,
    Role,
)
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from rag_contracts.enums import ContractType, SourceType
from rag_core.config import RagSettings
from rag_core.factory import health_check

router = APIRouter(
    prefix="/knowledge",
    tags=["Admin"],
    dependencies=[Depends(require_roles(Role.admin)), Depends(get_service_context)],
)


def load_manifest() -> dict:
    """只读取部署者配置的清单；不接收客户端文件路径，不公开磁盘路径。"""
    path = os.environ.get("CG_KNOWLEDGE_MANIFEST", "")
    if not path:
        return {"documents": [], "notice": "尚未配置官方初始库"}
    try:
        result = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(result.get("documents"), list):
            raise ValueError("invalid manifest")
        return result
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(503, "知识库来源清单暂时不可用") from exc


@router.get(
    "/sources",
    response_model=KnowledgeSourcesResponse,
    operation_id="getKnowledgeSources",
)
async def sources(
    context: Annotated[ServiceContext, Depends(get_service_context)],
) -> KnowledgeSourcesResponse:
    manifest = load_manifest()
    records = await StandardClauseRepository(context.session).list_all()
    cfg = RagSettings()
    llm = get_llm_settings()
    checks = await asyncio.to_thread(health_check, cfg)
    public_sources = [
        {
            key: entry.get(key)
            for key in (
                "doc_id",
                "library",
                "title",
                "code",
                "source_url",
                "published",
                "effective_date",
                "scope",
                "sha256",
                "article_count",
            )
        }
        for entry in manifest["documents"]
    ]
    data = {
        "sources": public_sources,
        "standardDocuments": sum(e["library"] == "standard" for e in public_sources),
        "lawDocuments": sum(e["library"] == "law" for e in public_sources),
        "lawArticles": sum(e.get("article_count") or 0 for e in public_sources),
        "enabledClauses": sum(record.is_enabled for record in records),
        "embedding": {
            "mode": cfg.embed_mode,
            "model": cfg.embed_model if cfg.embed_mode == "http" else "hash-trick-v1",
            "dimension": cfg.vector_dim,
            "status": checks["dense"],
        },
        "generation": {"provider": llm.provider, "model": llm.model},
        "notice": manifest.get("notice", "官方参考初始库，适用范围须人工核对"),
    }

    return build_success_response(KnowledgeSourcesResponse, data)


@router.get(
    "/search", response_model=KnowledgeSearchResponse, operation_id="searchKnowledge"
)
async def search(
    query: Annotated[str, Query(min_length=2, max_length=2000)],
    library: Literal["all", "clauses", "law"] = "all",
    contract_type: Annotated[ContractType | None, Query(alias="contractType")] = None,
) -> KnowledgeSearchResponse:
    source_types = {
        "all": [SourceType.STANDARD_CLAUSE, SourceType.LAW_ARTICLE],
        "clauses": [SourceType.STANDARD_CLAUSE],
        "law": [SourceType.LAW_ARTICLE],
    }[library]
    result = await asyncio.to_thread(
        build_rag_tool(),
        query,
        contract_type=contract_type,
        source_types=source_types,
        top_k=5,
        reason="knowledge_preview",
    )
    manifest = load_manifest()
    sources_by_code = {entry["code"]: entry for entry in manifest["documents"]}
    items = []
    for citation in result.citations:
        entry = next(
            (
                source
                for code, source in sources_by_code.items()
                if code in citation.sourceTitle
                or source["title"] in citation.sourceTitle
            ),
            None,
        )
        items.append(
            {
                **citation.model_dump(mode="json"),
                "sourceUrl": entry["source_url"] if entry else None,
                "scope": entry["scope"] if entry else "人工维护条款，需核对审批状态",
            }
        )
    data = {
        "status": result.status,
        "items": items,
        "warnings": result.warnings,
        "contextTokens": result.token_usage.prompt_tokens,
    }
    return build_success_response(KnowledgeSearchResponse, data)
