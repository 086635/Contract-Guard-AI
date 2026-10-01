"""C58 预览元数据与预览 API 测试。

离线部分用**注入替身**（合同/预览仓储、对象存储、任务派发器）覆盖状态机、
重试幂等与 URL 签发规则；真实 HTTP 路径（403 / 404 / 响应形状）在设置
`CG_POSTGRES_DSN` 时执行，未设置则按统一约定 skip。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_service_context
from app.api.v1 import contracts as contracts_router
from app.db.repository.contract import ContractDetail
from app.db.repository.preview import PreviewRecord
from app.generated.schemas import ContractStatus, PreviewStatus, Role, Status
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.ports import ProgressBrokerPort
from app.services.preview_service import (
    PREVIEW_URL_TTL_SECONDS,
    PreviewService,
    initial_preview_status,
    preview_object_key_for,
    source_kind_of,
)
from rag_contracts.enums import ContractType
from tests.service_testkit import (
    create_schema,
    drop_schema,
    install_test_auth,
    migrate,
    new_schema_name,
    schema_engine,
    service_context,
    session_factory,
)
from tests.test_api_real_endpoints import _dependency_calls

POSTGRES_DSN = os.getenv("CG_POSTGRES_DSN")
CONTRACT_ID = UUID("00000000-0000-0000-0000-0000000000c1")
# 路由注册在 contracts 子路由上（不带 /api/v1 前缀），此处用子路由路径定位。
PREVIEW_PATH = "/contracts/{contract_id}/preview"
RETRY_PATH = "/contracts/{contract_id}/preview/retry"


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------
class FakeObjectStorage:
    """对象存储替身：记录签名调用，**不把内部 key 拼进 URL**。"""

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, int]] = []

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        raise AssertionError("预览不签发 PUT URL")

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        self.get_calls.append((file_key, expires_s))
        return "https://minio.test/contract-previews?sig=stub"

    def stat_object(self, file_key: str) -> Any:
        raise AssertionError("预览不读对象 stat")


class FakeDispatcher:
    """Celery 派发替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def send(
        self,
        task_name: str,
        *,
        args: tuple[object, ...] = (),
        task_id: str | None = None,
    ) -> str:
        del task_id
        self.calls.append((str(task_name), args))
        return "celery-convert-1"

    def get_status(self, task_id: str) -> None:
        del task_id
        return None


class FakeSession:
    """只实现 commit 的 session 替身（服务层只在写路径调用 commit）。"""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeContractRepository:
    """按 ID 返回单一合同的仓储替身。"""

    def __init__(self, record: ContractDetail | None) -> None:
        self._record = record

    async def get(self, contract_id: UUID) -> ContractDetail | None:
        if self._record is not None and self._record.id == contract_id:
            return self._record
        return None


class FakePreviewRepository:
    """内存版预览仓储：只实现 C58 用到的三个方法。"""

    def __init__(self) -> None:
        self.rows: dict[UUID, PreviewRecord] = {}

    async def get_by_contract(self, contract_id: UUID) -> PreviewRecord | None:
        return next(
            (row for row in self.rows.values() if row.contract_id == contract_id),
            None,
        )

    async def create(
        self,
        contract_id: UUID,
        *,
        status: PreviewStatus,
        preview_object_key: str | None = None,
        error_message: str | None = None,
    ) -> PreviewRecord:
        now = datetime.now(UTC)
        record = PreviewRecord(
            id=uuid4(),
            contract_id=contract_id,
            status=PreviewStatus(status),
            preview_object_key=preview_object_key,
            convert_task_id=None,
            page_count=None,
            converter_version=None,
            error_message=error_message,
            created_at=now,
            updated_at=now,
        )
        self.rows[record.id] = record
        return record

    async def mark_running(
        self,
        preview_id: UUID,
        convert_task_id: str,
    ) -> PreviewRecord:
        old = self.rows[preview_id]
        updated = old.model_copy(
            update={
                "status": PreviewStatus.running,
                "convert_task_id": convert_task_id,
                "updated_at": datetime.now(UTC),
            }
        )
        self.rows[preview_id] = updated
        return updated


def _contract(
    filename: str = "采购合同.pdf",
    *,
    contract_id: UUID = CONTRACT_ID,
) -> ContractDetail:
    now = datetime.now(UTC)
    return ContractDetail(
        id=contract_id,
        filename=filename,
        file_size=2048,
        file_hash="hash-c58",
        storage_path=f"contracts/2026/10/{uuid4().hex}-{filename}",
        status=ContractStatus.parsing,
        contract_type=ContractType.PURCHASE,
        version=1,
        created_at=now,
        updated_at=now,
    )


def _service(
    contract: ContractDetail | None,
    *,
    previews: FakePreviewRepository | None = None,
    storage: FakeObjectStorage | None = None,
    dispatcher: FakeDispatcher | None = None,
) -> tuple[PreviewService, FakePreviewRepository, FakeObjectStorage, FakeDispatcher]:
    previews = previews if previews is not None else FakePreviewRepository()
    storage = storage if storage is not None else FakeObjectStorage()
    dispatcher = dispatcher if dispatcher is not None else FakeDispatcher()
    context = ServiceContext(
        session=cast(AsyncSession, FakeSession()),
        object_storage=storage,  # type: ignore[arg-type]
        task_dispatcher=dispatcher,  # type: ignore[arg-type]
        progress_broker=cast(ProgressBrokerPort, object()),
    )
    service = PreviewService(
        context,
        previews=previews,  # type: ignore[arg-type]
        contracts=FakeContractRepository(contract),  # type: ignore[arg-type]
    )
    return service, previews, storage, dispatcher


# --------------------------------------------------------------------------
# 1. 来源分类（纯函数）
# --------------------------------------------------------------------------
def test_source_kind_classification() -> None:
    """来源分类只认 .pdf/.docx（选择预览管线，不是上传安全判据）。"""
    assert source_kind_of("a.PDF") == "PDF"
    assert source_kind_of("dir/b.docx") == "DOCX"
    assert source_kind_of("c.txt") is None
    assert initial_preview_status("a.pdf") is PreviewStatus.done
    assert initial_preview_status("a.docx") is PreviewStatus.pending
    assert initial_preview_status("a.txt") is PreviewStatus.failed


# --------------------------------------------------------------------------
# 2. 断言 1：PDF 立即 DONE
# --------------------------------------------------------------------------
def test_preview_pdf_ready() -> None:
    """PDF：上传即可直接预览——初始 DONE + 原文对象 + 运行时签名 URL。"""
    contract = _contract("采购合同.pdf")
    service, previews, storage, _ = _service(contract)

    data = asyncio.run(service.get(contract.id))

    assert data.status is PreviewStatus.done
    assert data.preview_url is not None
    assert data.expires_in == PREVIEW_URL_TTL_SECONDS
    assert data.page_count is None and data.converter_version is None
    # 运行时签发：URL 由对象存储端口给出，且只签原文对象
    assert storage.get_calls == [(contract.storage_path, PREVIEW_URL_TTL_SECONDS)]
    assert preview_object_key_for(contract) == contract.storage_path
    assert len(previews.rows) == 1


def test_preview_pdf_get_is_idempotent() -> None:
    """重复 GET 不重复建行（一合同一行）。"""
    contract = _contract("采购合同.pdf")
    service, previews, _, _ = _service(contract)

    asyncio.run(service.get(contract.id))
    asyncio.run(service.get(contract.id))

    assert len(previews.rows) == 1


# --------------------------------------------------------------------------
# 3. 断言 2：DOCX 初始 PENDING
# --------------------------------------------------------------------------
def test_preview_docx_pending() -> None:
    """DOCX：初始 PENDING、无预览 URL、不触碰对象存储（等待 convert 队列）。"""
    contract = _contract("采购合同.docx")
    service, previews, storage, _ = _service(contract)

    data = asyncio.run(service.get(contract.id))

    assert data.status is PreviewStatus.pending
    assert data.preview_url is None
    assert data.expires_in == 0
    assert storage.get_calls == []
    row = next(iter(previews.rows.values()))
    assert row.preview_object_key is None


# --------------------------------------------------------------------------
# 4. 断言 3：retry 幂等
# --------------------------------------------------------------------------
def test_preview_retry_idempotent() -> None:
    """在飞转换任务存在时重复 retry 不重复派发，且返回同一个 taskId。"""
    contract = _contract("采购合同.docx")
    service, _, _, dispatcher = _service(contract)

    first = asyncio.run(service.retry(contract.id))
    second = asyncio.run(service.retry(contract.id))

    assert dispatcher.calls == [
        ("convert_doc", (contract.storage_path, str(contract.id)))
    ]
    assert first.task_id == second.task_id == "celery-convert-1"
    assert first.status is Status.running and second.status is Status.running


def test_preview_retry_skips_when_done() -> None:
    """已 DONE（PDF 直用原文）时 retry 不再派发转换。"""
    contract = _contract("采购合同.pdf")
    service, _, _, dispatcher = _service(contract)

    progress = asyncio.run(service.retry(contract.id))

    assert dispatcher.calls == []
    assert progress.status is Status.done
    assert progress.percent == 1.0


# --------------------------------------------------------------------------
# 5. 断言 5：URL 不暴露内部 key + 不存在不泄露
# --------------------------------------------------------------------------
def test_preview_dto_has_no_internal_key_field() -> None:
    """对外 DTO 字段固定，绝无 storagePath / previewObjectKey 之类的内部字段。"""
    contract = _contract("采购合同.pdf")
    service, _, _, _ = _service(contract)

    dumped = asyncio.run(service.get(contract.id)).model_dump(
        by_alias=True, mode="json"
    )

    assert set(dumped) == {
        "contractId",
        "status",
        "previewUrl",
        "expiresIn",
        "pageCount",
        "converterVersion",
        "createdAt",
    }
    assert not any(
        "key" in field.lower() or "path" in field.lower() for field in dumped
    )


def test_preview_missing_contract_raises_not_found() -> None:
    """合同不存在 → `ContractNotFoundError`（路由统一映射 404，不泄露存在性）。"""
    service, _, _, _ = _service(None)

    with pytest.raises(ContractNotFoundError):
        asyncio.run(service.get(CONTRACT_ID))
    with pytest.raises(ContractNotFoundError):
        asyncio.run(service.retry(CONTRACT_ID))


# --------------------------------------------------------------------------
# 6. 断言 4：路由与权限（结构断言，离线可跑）
# --------------------------------------------------------------------------
def _routes_by_path() -> dict[str, APIRoute]:
    """contracts 子路由的 path → APIRoute（依赖图可直接检查）。"""
    return {
        route.path: route
        for route in contracts_router.router.routes
        if isinstance(route, APIRoute)
    }


def _required_roles(dependency: object) -> set[Role] | None:
    """从 `require_roles(...)` 闭包中取出允许角色集合。"""
    for cell in getattr(dependency, "__closure__", None) or ():
        value = cell.cell_contents
        if isinstance(value, (set, frozenset)) and value:
            if all(isinstance(item, Role) for item in value):
                return set(value)
    return None


def test_preview_permission() -> None:
    """GET 需登录；retry 仅 ADMIN/REVIEWER，其它角色 403。"""
    routes = _routes_by_path()
    get_route = routes[PREVIEW_PATH]
    retry_route = routes[RETRY_PATH]

    assert get_current_user in _dependency_calls(get_route.dependant)

    retry_deps = _dependency_calls(retry_route.dependant)
    assert get_current_user in retry_deps
    role_gates = [
        roles for roles in map(_required_roles, retry_deps) if roles is not None
    ]
    assert role_gates == [{Role.admin, Role.reviewer}]


def test_preview_retry_requires_request_id_header() -> None:
    """retry 必须带 X-Request-ID（幂等键），否则 422。"""
    header_aliases = {
        str(parameter.alias).lower()
        for parameter in _routes_by_path()[RETRY_PATH].dependant.header_params
    }
    assert "x-request-id" in header_aliases


# --------------------------------------------------------------------------
# 7. 真实 HTTP 路径（需要 PostgreSQL）
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not POSTGRES_DSN,
    reason="未设置 CG_POSTGRES_DSN，跳过 C58 预览 HTTP 集成测试",
)
def test_preview_routes_http_contract() -> None:
    assert POSTGRES_DSN is not None
    asyncio.run(_exercise_preview_http(POSTGRES_DSN))


async def _exercise_preview_http(dsn: str) -> None:
    schema = new_schema_name("cg_c58")
    await create_schema(dsn, schema)
    engine = schema_engine(dsn, schema)
    factory = session_factory(engine)
    try:
        await migrate(dsn, schema)
        async with factory() as session:
            contract_id = uuid4()
            contract = ContractDetail(
                id=contract_id,
                filename="采购合同.pdf",
                file_size=2048,
                file_hash="hash-c58-http",
                storage_path="contracts/2026/10/abc-采购合同.pdf",
                status=ContractStatus.parsing,
                contract_type=ContractType.PURCHASE,
                version=1,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            await _insert_contract(session, contract)

            storage = FakeObjectStorage()
            dispatcher = FakeDispatcher()
            context = service_context(
                session,
                object_storage=storage,  # type: ignore[arg-type]
                task_dispatcher=dispatcher,  # type: ignore[arg-type]
            )
            app = FastAPI()
            app.include_router(contracts_router.router, prefix="/api/v1")
            install_test_auth(app, session, role=Role.viewer)
            app.dependency_overrides[get_service_context] = lambda: context
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                # 1) 存在合同：PDF 立即 DONE，且响应不含内部 key
                ok = await client.get(f"/api/v1/contracts/{contract_id}/preview")
                assert ok.status_code == 200, ok.text
                payload = ok.json()["data"]
                assert payload["status"] == "DONE"
                assert payload["previewUrl"]
                assert (
                    "previewObjectKey" not in payload and "storagePath" not in payload
                )

                # 2) 不存在的合同：404（不泄露存在性）
                missing = await client.get(f"/api/v1/contracts/{uuid4()}/preview")
                assert missing.status_code == 404

                # 3) VIEWER 触发 retry：403
                denied = await client.post(
                    f"/api/v1/contracts/{contract_id}/preview/retry",
                    headers={"X-Request-ID": str(uuid4())},
                )
                assert denied.status_code == 403
                assert dispatcher.calls == []
    finally:
        await engine.dispose()
        await drop_schema(dsn, schema)


async def _insert_contract(session: AsyncSession, contract: ContractDetail) -> None:
    """把合同投影写入真实表（HTTP 用例的前置数据）。"""
    from app.db.models import Contract

    session.add(
        Contract(
            id=contract.id,
            filename=contract.filename,
            file_size=contract.file_size,
            file_hash=contract.file_hash,
            storage_path=contract.storage_path,
            status=contract.status,
            contract_type=contract.contract_type,
            version=contract.version,
            created_at=contract.created_at,
            updated_at=contract.updated_at,
        )
    )
    await session.commit()
