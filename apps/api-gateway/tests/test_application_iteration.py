"""T-D6: real persistence, permissions and parse recovery regression."""
from __future__ import annotations

import asyncio
import csv
import io
import os
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_service_context
from app.api.v1 import config, contracts, feedback
from app.config import settings
from app.db.repository.config_feedback import FeedbackRepository
from app.db.repository.contract import (
    ContractCreate,
    ContractRepository,
    TaskRepository,
)
from app.generated.schemas import ContractStatus, FeedbackVerdict, Role, TargetType
from app.services import feedback_service
from app.services.ports import ObjectStoragePort, StoredObjectInfo, TaskStatusSnapshot
from tests.service_testkit import (
    create_schema,
    drop_schema,
    install_test_auth,
    migrate,
    new_schema_name,
    rule_body,
    schema_engine,
    service_context,
    session_factory,
)

DSN = os.getenv("CG_POSTGRES_DSN")

class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.snapshots: dict[str, TaskStatusSnapshot] = {}

    def send(self, task_name: str, *, args: tuple[object, ...] = (), task_id: str | None = None) -> str:
        self.calls.append((str(task_name), args))
        return f"iteration-{len(self.calls)}"

    def get_status(self, task_id: str) -> TaskStatusSnapshot | None:
        return self.snapshots.get(task_id)

class Storage:
    size = 128
    def stat_object(self, file_key: str) -> StoredObjectInfo:
        return StoredObjectInfo(self.size, "test-etag")

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        return 'http://storage.test/put/' + file_key

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        return 'http://storage.test/get/' + file_key

@pytest.mark.skipif(not DSN, reason="requires isolated PostgreSQL")
@pytest.mark.parametrize("scenario", ["rules", "retry", "export"])
def test_application_persistence_and_permissions(scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert DSN
    monkeypatch.setattr(settings, "upload_av_mode", "off")
    asyncio.run(exercise(DSN, scenario, monkeypatch))

async def exercise(dsn: str, scenario: str, monkeypatch: pytest.MonkeyPatch) -> None:
    schema = new_schema_name("cg_iteration")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    try:
        await migrate(dsn, schema)
        async with session_factory(engine)() as session:
            dispatcher, storage = Dispatcher(), Storage()
            context = service_context(session, object_storage=cast(ObjectStoragePort, storage), task_dispatcher=dispatcher)
            app = FastAPI()
            app.include_router(config.router, prefix="/api/v1")
            app.include_router(contracts.router, prefix="/api/v1")
            app.include_router(feedback.router, prefix="/api/v1")
            user = install_test_auth(app, session, persist_user=True)
            app.dependency_overrides[get_service_context] = lambda: context
            await session.commit()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                if scenario == "rules":
                    body = rule_body()
                    body["promptConfig"]["temperature"] = .7
                    body["promptConfig"]["riskToleranceLevel"] = "STRICT"
                    body["enabledRules"] = ["payment_term_risk", "ip_ownership_risk"]
                    body["customEntities"] = [{"entityKey": "warranty", "entityName": "质保期", "description": "提取明确约定的质保期限", "isRequired": True}]
                    for role in (Role.viewer, Role.reviewer):
                        install_test_auth(app, session, role=role)
                        assert (await client.post("/api/v1/settings/rules", json=body)).status_code == 403
                    install_test_auth(app, session)
                    created = await client.post("/api/v1/settings/rules", json=body)
                    assert created.status_code == 201, created.text
                    data = created.json()["data"]
                    assert data["version"] == 1
                    assert data["promptConfig"] == body["promptConfig"]
                    assert data["enabledRules"] == body["enabledRules"]
                    assert data["customEntities"] == body["customEntities"]
                    assert data["updatedAt"] != body["updatedAt"]
                    url = f'/api/v1/settings/rules/{data["id"]}'
                    assert (await client.get(url)).json()["data"] == data
                    duplicate = {**body, "id": str(uuid4())}
                    assert (await client.post("/api/v1/settings/rules", json=duplicate)).status_code == 409
                    changed = {**data, "enabled": False, "contractType": "NDA"}
                    changed["promptConfig"] = {**data["promptConfig"], "systemPrompt": "复核保密期限", "temperature": .3}
                    updated = await client.put(url, json=changed)
                    assert updated.status_code == 200, updated.text
                    result = updated.json()["data"]
                    assert result["version"] == 2
                    assert result["enabled"] is False
                    assert result["promptConfig"] == changed["promptConfig"]
                    assert result["contractType"] == "NDA"
                    assert result["customEntities"] == body["customEntities"]
                    assert (await client.put(url, json=changed)).status_code == 409
                    assert (await client.get(url)).json()["data"] == result
                    install_test_auth(app, session, role=Role.reviewer)
                    assert (await client.put(url, json=result)).status_code == 403
                elif scenario == "export":
                    stored = await ContractRepository(session).create(ContractCreate(filename="标注原文件.pdf", file_size=128, file_hash="b" * 64, storage_path="original.pdf", status=ContractStatus.pending_review, created_by_id=user.id))
                    repo = FeedbackRepository(session)
                    for index in range(101):
                        await repo.create(TargetType.element, f"clause-{index:04d}", FeedbackVerdict.correct, '=SUM(1,2)' if index == 100 else '真实补充说明', contract_id=stored.id, reviewer='审核人')
                    await session.commit()
                    uploads: list[tuple[str, bytes]] = []
                    fail = False

                    class ExportClient:
                        def __init__(self, **kwargs):
                            pass

                        async def __aenter__(self):
                            return self

                        async def __aexit__(self, *args):
                            pass

                        async def put(self, url, *, content, headers):
                            if fail:
                                raise OSError('internal-upload-secret')
                            assert headers['Content-Type'] == 'text/csv; charset=utf-8'
                            uploads.append((url, content))
                            return httpx.Response(200, request=httpx.Request('PUT', url))

                    monkeypatch.setattr(feedback_service.httpx, 'AsyncClient', ExportClient)
                    for role in (Role.viewer, Role.reviewer):
                        install_test_auth(app, session, role=role)
                        assert (await client.post('/api/v1/feedback/export')).status_code == 403
                    assert not uploads
                    install_test_auth(app, session)
                    exported = await client.post('/api/v1/feedback/export')
                    assert exported.status_code == 200, exported.text
                    data = exported.json()['data']
                    assert data['recordCount'] == 101 and data['downloadUrl'].startswith('http://storage.test/get/feedback-exports/')
                    assert len(uploads) == 1 and uploads[0][1].startswith(b'\xef\xbb\xbf')
                    rows = list(csv.reader(io.StringIO(uploads[0][1].decode('utf-8-sig'))))
                    assert len(rows) == 102
                    assert rows[0][5] == '用户修正'
                    assert all(row[2] == '条款' for row in rows[1:])
                    assert "'=SUM(1,2)" in [row[5] for row in rows[1:]]
                    assert '真实补充说明' in [row[5] for row in rows[1:]]
                    fail = True
                    rejected = await client.post('/api/v1/feedback/export')
                    assert rejected.status_code == 502
                    assert 'internal-upload-secret' not in rejected.text
                else:
                    repo, tasks = ContractRepository(session), TaskRepository(session)
                    stored = await repo.create(ContractCreate(filename="原文件.pdf", file_size=128, file_hash="a" * 64, storage_path=f"contracts/2026/10/{'a' * 32}-original.pdf", status=ContractStatus.parsing, created_by_id=user.id))
                    await session.commit()
                    url = f"/api/v1/contracts/{stored.id}/parse/retry"
                    install_test_auth(app, session, role=Role.viewer)
                    assert (await client.post(url, json={})).status_code == 403
                    install_test_auth(app, session, role=Role.reviewer)
                    first = await client.post(url, json={})
                    assert first.status_code == 202, first.text
                    second = await client.post(url, json={})
                    assert second.json()["data"]["taskId"] == first.json()["data"]["taskId"]
                    assert len(dispatcher.calls) == 1
                    assert dispatcher.calls[0][1] == (stored.storage_path, str(stored.id))
                    assert (await client.post(f"/api/v1/contracts/{uuid4()}/parse/retry", json={})).status_code == 404
                    await repo.update_status(stored.id, ContractStatus.finalized)
                    await session.commit()
                    assert (await client.post(url, json={})).status_code == 409
                    await repo.update_status(stored.id, ContractStatus.parsing)
                    await session.commit()
                    dispatcher.snapshots["iteration-1"] = TaskStatusSnapshot("DONE", "failed", 1, {"status": "FAILED", "warnings": ["图像处理失败"]})
                    status = await client.get(f"/api/v1/contracts/{stored.id}/parse-status")
                    assert status.status_code == 200, status.text
                    assert status.json()["data"]["status"] == "FAILED"
                    assert status.json()["data"]["result"]["errorMessage"] == "图像处理失败"
                    storage.size = 129
                    assert (await client.post(url, json={})).status_code == 409
                    storage.size = 128
                    monkeypatch.setattr(settings, "upload_av_mode", "clamav")
                    scanned = await client.post(url, json={})
                    assert scanned.status_code == 202, scanned.text
                    assert dispatcher.calls[-1][0] == "scan_upload"
                    scan_id = scanned.json()["data"]["taskId"]
                    dispatcher.snapshots["iteration-2"] = TaskStatusSnapshot("DONE", "finished", 1, {"status": "SUCCESS", "parseTaskId": "scan-child-parser"})
                    child = await client.post(url, json={})
                    assert child.status_code == 202, child.text
                    assert child.json()["data"]["taskId"] != scan_id
                    assert len(dispatcher.calls) == 2  # child already dispatched by worker; do not rescan
                    record = await tasks.get(UUID(child.json()["data"]["taskId"]))
                    assert record and record.celery_task_id == "scan-child-parser"
                    dispatcher.snapshots["scan-child-parser"] = TaskStatusSnapshot("DONE", "finished", 1, {"status": "SUCCESS", "clauses": [], "elements": [], "sealDetections": []})
                    done = await client.get(f"/api/v1/contracts/{stored.id}/parse-status")
                    assert done.json()["data"]["status"] == "DONE"
                    assert done.json()["data"]["result"]["auditStatus"] == "NOT_STARTED"
                    assert (await repo.get(stored.id)).status == ContractStatus.pending_review
                    await tasks.create(stored.id, "RUN_AUDIT", "offline-audit")
                    await session.commit()
                    monkeypatch.setenv("CG_LLM_PROVIDER", "offline")
                    dispatcher.snapshots["scan-child-parser"] = TaskStatusSnapshot("DONE", "finished", 1, {"status": "SUCCESS", "clauses": [{"clause_id": "clause-0001", "text": "第一条付款"}], "lineCount": 3})
                    dispatcher.snapshots["offline-audit"] = TaskStatusSnapshot("DONE", "finished", 1, {"status": "FAILED", "warnings": ["ERR_LLM_INVALID_RISK"]})
                    degraded = await client.get(f"/api/v1/contracts/{stored.id}/parse-status")
                    assert degraded.status_code == 200, degraded.text
                    assert degraded.json()["data"]["result"]["auditStatus"] == "FAILED"
                    assert "离线模型" in degraded.json()["data"]["result"]["auditMessage"]
                    assert degraded.json()["data"]["result"]["clauseCount"] == 1
                    # Foreign ownership remains hidden from non-admin retries.
                    foreign = await repo.create(ContractCreate(filename="other.pdf", file_size=128, file_hash="b" * 64, storage_path=stored.storage_path, created_by_id=None))
                    await session.commit()
                    assert (await client.post(f"/api/v1/contracts/{foreign.id}/parse/retry", json={})).status_code == 404
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)
