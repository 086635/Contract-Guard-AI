"""C70 标准条款库服务/路由测试（离线 SQLite + 内存向量库）。

覆盖 C70 §⑧：CRUD、停用不命中、重建幂等、ADMIN-only。
不依赖真实 PostgreSQL / Qdrant：索引侧用 rag_core 的内存实现，
真实 PG 往返由 `CG_POSTGRES_DSN` 集成用例与 C74 批级验收覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ai_engine.agents.tools import RagTool
from app.api.deps import get_service_context, get_standard_clause_service
from app.api.v1 import standard_clauses as standard_clauses_router
from app.db.models import Base
from app.generated.schemas import Role
from app.services.standard_clause_service import (
    StandardClauseIndexer,
    StandardClauseService,
)
from cg_observability import TokenCounter
from rag_contracts.enums import SourceType
from rag_core.adapters import HashEmbedder, InMemoryVectorStore, NoopReranker
from rag_core.config import RagSettings
from rag_core.service import RAGService
from tests.service_testkit import install_test_auth, service_context

API = "/api/v1/admin/standard-clauses"
CLAUSE_BODY = {
    "title": "逾期付款违约金",
    "contractType": "PURCHASE",
    "text": "买方逾期付款的，应按日支付合同总价千分之五的违约金。",
}
SECOND_BODY = {
    "title": "保密义务",
    "contractType": "PURCHASE",
    "text": "双方对商业秘密负有保密义务，保密期限为合同终止后三年。",
}


class _BrokenIndexer:
    """索引不可用替身：用于验证元数据事务回滚（503）。"""

    def upsert(self, records: object) -> int:
        raise RuntimeError("vector store down")

    def remove(self, clause_id: object) -> int:
        raise RuntimeError("vector store down")

    def health(self) -> bool:
        return False


@asynccontextmanager
async def _harness(
    *,
    role: Role = Role.admin,
    indexer: object | None = None,
) -> AsyncIterator[SimpleNamespace]:
    """组装 SQLite + 内存索引 + FastAPI 路由的离线测试环境。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session = factory()

    embedder = HashEmbedder(dim=1024, model_name="hash-test")
    store = InMemoryVectorStore()
    active_indexer = indexer or StandardClauseIndexer(embedder, store)
    service = StandardClauseService(session, indexer=active_indexer)

    app = FastAPI()
    app.include_router(standard_clauses_router.router, prefix="/api/v1")
    install_test_auth(app, session, role=role)
    app.dependency_overrides[get_service_context] = lambda: service_context(session)
    app.dependency_overrides[get_standard_clause_service] = lambda: service

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield SimpleNamespace(
                client=client,
                session=session,
                service=service,
                store=store,
                embedder=embedder,
            )
    finally:
        app.dependency_overrides.clear()
        await session.close()
        await engine.dispose()


def _retrieve(store: InMemoryVectorStore, text: str):  # type: ignore[no-untyped-def]
    """走唯一 RAG 入口检索（与生产同一条路径）。"""
    embedder = HashEmbedder(dim=1024, model_name="hash-test")
    service = RAGService(
        embedder=embedder,
        vector_store=store,
        counter=TokenCounter(force_estimate=True),
        reranker=NoopReranker(),
        dense_top=20,
        default_max_context_tokens=3000,
    )
    tool = RagTool(service=service, settings=RagSettings(_env_file=None))
    return tool(text, source_types=[SourceType.STANDARD_CLAUSE], top_k=3)


def test_standard_clause_crud() -> None:
    """CRUD 持久化 + 乐观锁 409 + 空更新 400 + 不存在 404。"""
    asyncio.run(_exercise_crud())


async def _exercise_crud() -> None:
    async with _harness() as env:
        created = await env.client.post(API, json=CLAUSE_BODY)
        assert created.status_code == 201, created.text
        clause = created.json()["data"]
        assert clause["isEnabled"] is True
        assert clause["version"] == 1
        assert clause["title"] == CLAUSE_BODY["title"]

        listed = await env.client.get(API, params={"page": 1, "pageSize": 20})
        assert listed.status_code == 200, listed.text
        data = listed.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == clause["id"]

        patched = await env.client.patch(
            f"{API}/{clause['id']}",
            json={"title": "逾期付款违约金（新版）", "version": 1},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["data"]["title"] == "逾期付款违约金（新版）"
        assert patched.json()["data"]["version"] == 2

        stale = await env.client.patch(
            f"{API}/{clause['id']}",
            json={"title": "并发写", "version": 1},
        )
        assert stale.status_code == 409

        empty = await env.client.patch(
            f"{API}/{clause['id']}",
            json={"version": 2},
        )
        assert empty.status_code == 400

        missing = await env.client.patch(
            f"{API}/{uuid4()}",
            json={"title": "不存在", "version": 1},
        )
        assert missing.status_code == 404

        filtered = await env.client.get(
            API, params={"query": "违约金", "contractType": "PURCHASE"}
        )
        assert filtered.status_code == 200
        assert filtered.json()["data"]["total"] == 1


def test_disabled_clause_not_retrieved() -> None:
    """停用后不再命中生产检索，重新启用后可再次命中（软停用可恢复）。"""
    asyncio.run(_exercise_disabled())


async def _exercise_disabled() -> None:
    async with _harness() as env:
        clause = (await env.client.post(API, json=CLAUSE_BODY)).json()["data"]
        assert _retrieve(env.store, CLAUSE_BODY["text"]).status == "OK"

        disabled = await env.client.patch(
            f"{API}/{clause['id']}", json={"isEnabled": False, "version": 1}
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["data"]["isEnabled"] is False
        assert env.store.count() == 0
        after = _retrieve(env.store, CLAUSE_BODY["text"])
        assert not any(
            CLAUSE_BODY["text"] in item.contentChunk for item in after.citations
        )

        restored = await env.client.patch(
            f"{API}/{clause['id']}", json={"isEnabled": True, "version": 2}
        )
        assert restored.status_code == 200, restored.text
        assert env.store.count() == 1
        again = _retrieve(env.store, CLAUSE_BODY["text"])
        assert any(CLAUSE_BODY["text"] in item.contentChunk for item in again.citations)


def test_rebuild_index_idempotent() -> None:
    """全量重建幂等：重复执行结果一致，停用条目不在索引内。"""
    asyncio.run(_exercise_rebuild())


async def _exercise_rebuild() -> None:
    async with _harness() as env:
        await env.client.post(API, json=CLAUSE_BODY)
        second = (await env.client.post(API, json=SECOND_BODY)).json()["data"]
        await env.client.patch(
            f"{API}/{second['id']}", json={"isEnabled": False, "version": 1}
        )
        assert env.store.count() == 1

        first = await env.client.post(f"{API}/rebuild")
        assert first.status_code == 200, first.text
        payload = first.json()["data"]
        assert payload["status"] == "DONE"
        assert payload["result"]["enabled"] == 1
        assert payload["result"]["removed"] == 1
        count_first = env.store.count()

        second_run = await env.client.post(f"{API}/rebuild")
        assert second_run.status_code == 200
        assert second_run.json()["data"]["result"] == payload["result"]
        assert env.store.count() == count_first == 1


def test_standard_clause_admin_only() -> None:
    """非 ADMIN 一律 403（读、写、启停、重建四个动作）。"""
    asyncio.run(_exercise_admin_only())


async def _exercise_admin_only() -> None:
    async with _harness(role=Role.viewer) as env:
        assert (await env.client.get(API)).status_code == 403
        assert (await env.client.post(API, json=CLAUSE_BODY)).status_code == 403
        assert (await env.client.post(f"{API}/rebuild")).status_code == 403
        denied = await env.client.patch(
            f"{API}/{uuid4()}", json={"title": "越权", "version": 1}
        )
        assert denied.status_code == 403
        assert env.store.count() == 0


def test_standard_clause_index_failure_rolls_back() -> None:
    """索引不可用时整体失败（503）且元数据不落库，可重试。"""
    asyncio.run(_exercise_index_failure())


async def _exercise_index_failure() -> None:
    async with _harness(indexer=_BrokenIndexer()) as env:
        failed = await env.client.post(API, json=CLAUSE_BODY)
        assert failed.status_code == 503, failed.text
        listed = await env.client.get(API)
        assert listed.status_code == 200
        assert listed.json()["data"]["total"] == 0
