from __future__ import annotations

import asyncio

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.v1 import knowledge
from rag_contracts.agent_tool import AgentRAGToolResult
from rag_contracts.enums import ContractType, SourceType
from rag_contracts.query import CitationOut


def test_knowledge_does_not_allow_anonymous_access():
    async def run():
        app = FastAPI()
        app.include_router(knowledge.router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/knowledge/sources")).status_code == 401
            assert (
                await client.get("/knowledge/search?query=付款责任")
            ).status_code == 401

    asyncio.run(run())


def test_search_returns_actual_citations_with_official_sources(monkeypatch):
    calls = []
    citation = CitationOut(
        sourceType=SourceType.LAW_ARTICLE,
        sourceTitle="中华人民共和国民法典 第五百七十七条",
        contentChunk="不履行合同义务应承担责任。",
        relevanceScore=0.8,
    )

    def tool(query, **kwargs):
        calls.append((query, kwargs))
        return AgentRAGToolResult(
            status="OK", context_block="法条原文", citations=[citation], trace_id="test"
        )

    monkeypatch.setattr(knowledge, "build_rag_tool", lambda: tool)
    monkeypatch.setattr(
        knowledge,
        "load_manifest",
        lambda: {
            "documents": [
                {
                    "code": "CIVIL_CODE",
                    "title": "中华人民共和国民法典",
                    "source_url": "https://www.gov.cn/law",
                    "scope": "全国",
                },
            ]
        },
    )

    async def run():
        app = FastAPI()
        app.include_router(knowledge.router)
        app.dependency_overrides[knowledge.router.dependencies[0].dependency] = lambda: (
            None
        )
        app.dependency_overrides[knowledge.get_service_context] = lambda: None
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/knowledge/search",
                params={
                    "query": "付款责任",
                    "library": "law",
                    "contractType": "PURCHASE",
                },
            )
            assert response.status_code == 200
            item = response.json()["data"]["items"][0]
            assert item["sourceUrl"] == "https://www.gov.cn/law"
            assert item["contentChunk"] == citation.contentChunk
            assert item["relevanceScore"] == 0.8
            assert (await client.get("/knowledge/search?query=x")).status_code == 422
            assert (
                await client.get("/knowledge/search?query=付款&library=fake")
            ).status_code == 422

    asyncio.run(run())
    assert calls[0][1]["source_types"] == [SourceType.LAW_ARTICLE]
    assert calls[0][1]["contract_type"] == ContractType.PURCHASE


def test_missing_and_broken_manifest_are_explicit(monkeypatch, tmp_path):
    monkeypatch.delenv("CG_KNOWLEDGE_MANIFEST", raising=False)
    assert knowledge.load_manifest()["documents"] == []
    monkeypatch.setenv("CG_KNOWLEDGE_MANIFEST", str(tmp_path / "missing.json"))
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        knowledge.load_manifest()
    assert error.value.status_code == 503
    assert str(tmp_path) not in str(error.value.detail)
