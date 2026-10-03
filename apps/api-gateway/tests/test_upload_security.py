"""C56 服务端上传大小 / 类型 / Key 约束测试。

离线用例：不依赖 PostgreSQL / MinIO / Celery——presign 与 upload 的**拒绝分支**
都在触碰数据库之前发生，因此只需要对象存储端口替身即可覆盖四条验收断言：

1. 超限拒绝（413）
2. 任意 key 拒绝（400）
3. stat size / ETag 与声明不一致拒绝（409）
4. presign 回传 contentType 且前端仍提交 sizeBytes

再加扩展名↔声明类型一致性（415）与 production 必填 sizeBytes（400）。
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.generated.schemas import FileType, UploadBody
from app.services import contract_service as contract_service_module
from app.services.context import ServiceContext
from app.services.contract_service import (
    MAX_UPLOAD_BYTES,
    ContractService,
    PresignRequest,
    _dispatch_target,
    generate_file_key,
)
from app.services.dispatch import TaskKind
from app.services.ports import (
    ProgressBrokerPort,
    StoredObjectInfo,
    TaskDispatcherPort,
)
from app.services.storage import UploadObjectError, validate_upload_object


class FakeObjectStorage:
    """对象存储端口替身：记录调用，可配置 stat 结果。"""

    def __init__(self, *, size: int = 4096, etag: str = "abc123") -> None:
        self.size = size
        self.etag = etag
        self.put_calls: list[tuple[str, int]] = []
        self.stat_calls: list[str] = []

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        self.put_calls.append((file_key, expires_s))
        return f"https://minio.test/{file_key}?method=PUT"

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        return f"https://minio.test/{file_key}?method=GET"

    def stat_object(self, file_key: str) -> StoredObjectInfo:
        self.stat_calls.append(file_key)
        return StoredObjectInfo(size=self.size, etag=self.etag)


def _service(storage: FakeObjectStorage) -> ContractService:
    """构造只用端口替身的 ContractService。

    presign / 拒绝分支不触碰 session，故 session 用占位对象即可（不产生 DB 访问）。
    """
    context = ServiceContext(
        session=cast(AsyncSession, object()),
        object_storage=storage,
        task_dispatcher=cast(TaskDispatcherPort, object()),
        progress_broker=cast(ProgressBrokerPort, object()),
    )
    return ContractService(context)


def _rejected(callable_) -> UploadObjectError:
    with pytest.raises(UploadObjectError) as excinfo:
        callable_()
    return excinfo.value


def test_size_limit_server_side() -> None:
    """声明大小超过 50 MB → 413，且不得签发 presigned URL。"""
    storage = FakeObjectStorage()
    service = _service(storage)

    error = _rejected(
        lambda: asyncio.run(
            service.presign_upload(
                PresignRequest(
                    fileName="spec.pdf",
                    fileType=FileType.pdf_native,
                    sizeBytes=MAX_UPLOAD_BYTES + 1,
                )
            )
        )
    )

    assert error.status_code == 413
    assert storage.put_calls == []


def test_arbitrary_key_rejected() -> None:
    """客户端自带任意 key（非服务端生成形态）→ 400，且不得访问对象存储。"""
    storage = FakeObjectStorage()
    service = _service(storage)

    error = _rejected(
        lambda: asyncio.run(
            service.upload(
                UploadBody(
                    fileKey="../reports/2026/10/steal.docx",
                    fileName="steal.docx",
                    fileType=FileType.docx,
                )
            )
        )
    )

    assert error.status_code == 400
    assert storage.stat_calls == []


def test_validate_upload_object_rejects_foreign_prefix() -> None:
    """storage 复核 helper 单独复核前缀：reports/ 前缀同样拒绝。"""
    error = _rejected(
        lambda: validate_upload_object(
            "reports/2026/10/whatever.docx",
            StoredObjectInfo(size=10, etag="e"),
        )
    )
    assert error.status_code == 400


def test_etag_mismatch() -> None:
    """stat ETag 与声明不一致 → 409（大小一致，只有 ETag 不符）。"""
    storage = FakeObjectStorage(size=2048, etag="real-etag")
    service = _service(storage)
    file_key = generate_file_key("spec.pdf")

    error = _rejected(
        lambda: asyncio.run(
            service.upload(
                UploadBody(
                    fileKey=file_key,
                    fileName="spec.pdf",
                    fileType=FileType.pdf_native,
                    sizeBytes=2048,
                    etag="forged-etag",
                )
            )
        )
    )

    assert error.status_code == 409
    assert storage.stat_calls == [file_key]


def test_stat_size_mismatch_rejected() -> None:
    """stat 大小与声明不一致 → 409（防止声明值被用来绕过准入）。"""
    storage = FakeObjectStorage(size=MAX_UPLOAD_BYTES, etag="abc123")
    service = _service(storage)
    file_key = generate_file_key("spec.docx")

    error = _rejected(
        lambda: asyncio.run(
            service.upload(
                UploadBody(
                    fileKey=file_key,
                    fileName="spec.docx",
                    fileType=FileType.docx,
                    sizeBytes=1024,
                )
            )
        )
    )

    assert error.status_code == 409


def test_presign_content_type_propagated() -> None:
    """contentType 随 presign 回传，并绑定到服务端生成的对象键。"""
    storage = FakeObjectStorage()
    service = _service(storage)
    content_type = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    data = asyncio.run(
        service.presign_upload(
            PresignRequest(
                fileName="采购合同.docx",
                fileType=FileType.docx,
                sizeBytes=1024,
                contentType=content_type,
            )
        )
    )

    assert data.content_type == content_type
    assert data.file_key.startswith("contracts/")
    assert data.file_key.endswith("-采购合同.docx")
    assert storage.put_calls == [(data.file_key, 600)]


def test_extension_content_mismatch() -> None:
    """.pdf 文件名 + DOCX 声明 → 415，且不得签发 presigned URL。"""
    storage = FakeObjectStorage()
    service = _service(storage)

    error = _rejected(
        lambda: asyncio.run(
            service.presign_upload(
                PresignRequest(fileName="spec.pdf", fileType=FileType.docx)
            )
        )
    )

    assert error.status_code == 415
    assert storage.put_calls == []


def test_unsupported_file_type_rejected() -> None:
    """声明类型与扩展名不自洽 → 415。

    ⚠️ T-D8-2 起 `FileType.image` **已放行**，故本用例改用「声明 PDF 但文件名是 .png」
    这一真正不自洽的组合；IMAGE 的放行由下一条用例覆盖。
    """
    storage = FakeObjectStorage()
    service = _service(storage)

    error = _rejected(
        lambda: asyncio.run(
            service.presign_upload(
                PresignRequest(fileName="scan.png", fileType=FileType.pdf_native)
            )
        )
    )

    assert error.status_code == 415


def test_image_file_type_accepted() -> None:
    """T-D8-2：`IMAGE` 声明 + 图片扩展名放行（真实类型仍由 C57 魔数判定）。"""
    storage = FakeObjectStorage()
    service = _service(storage)

    data = asyncio.run(
        service.presign_upload(
            PresignRequest(fileName="scan.png", fileType=FileType.image)
        )
    )

    assert data.file_key


def test_production_requires_size_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """production 未提供 sizeBytes → 400（development/test 仍可省略）。"""
    monkeypatch.setattr(contract_service_module.settings, "runtime_env", "production")
    storage = FakeObjectStorage()
    service = _service(storage)

    error = _rejected(
        lambda: asyncio.run(
            service.presign_upload(
                PresignRequest(fileName="spec.pdf", fileType=FileType.pdf_native)
            )
        )
    )

    assert error.status_code == 400
    assert storage.put_calls == []


@pytest.mark.parametrize(
    ("file_name", "file_type"),
    [("spec.pdf", FileType.pdf_native), ("spec.docx", FileType.docx)],
)
def test_compliant_declarations_pass(
    file_name: str,
    file_type: FileType,
) -> None:
    """合规声明（PDF / DOCX + 合法大小）通过 presign 并生成服务端 key。"""
    storage = FakeObjectStorage()
    service = _service(storage)

    data = asyncio.run(
        service.presign_upload(
            PresignRequest(fileName=file_name, fileType=file_type, sizeBytes=1024)
        )
    )

    assert data.file_key.startswith("contracts/")
    assert data.file_key.endswith(f"-{file_name}")
    assert data.expires_in == 600
    assert storage.put_calls == [(data.file_key, 600)]


# ---------------------------------------------------------------------------
# C57：`CG_UPLOAD_AV_MODE` 决定上传后的首跳任务（upload 流程接线）
# ---------------------------------------------------------------------------


def _upload_body(file_name: str = "spec.docx") -> UploadBody:
    return UploadBody(
        fileKey=generate_file_key(file_name),
        fileName=file_name,
        fileType=FileType.docx,
    )


def test_av_mode_off_dispatches_parse_first() -> None:
    """development/test 默认 off：保持原行为——直接派发 `parse_doc`。"""
    body = _upload_body()

    kind, args = _dispatch_target(body, "contract-1")

    assert kind is TaskKind.PARSE_DOC
    assert args == (body.file_key, "contract-1")


def test_av_mode_clamav_dispatches_scan_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CG_UPLOAD_AV_MODE=clamav`：首跳 `scan_upload`，由扫描通过后再链式派发 parse。"""
    monkeypatch.setattr(contract_service_module.settings, "upload_av_mode", "clamav")
    body = _upload_body()

    kind, args = _dispatch_target(body, "contract-1")

    assert kind is TaskKind.SCAN_UPLOAD
    assert args == (body.file_key, "spec.docx", "DOCX", "contract-1")


def test_scan_upload_is_registered_task_kind() -> None:
    """`scan_upload` 必须是网关允许派发的登记任务名（防拼写漂移）。"""
    assert TaskKind("scan_upload") is TaskKind.SCAN_UPLOAD


# ---------------------------------------------------------------------------
# 重复文件（同哈希）→ 409，而不是让唯一约束冒泡成 500
# ---------------------------------------------------------------------------
def test_duplicate_file_hash_rejected_as_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一文件重复上传 → 409 `ERR_FILE_ALREADY_UPLOADED`（对象复核之后、写库之前）。"""
    storage = FakeObjectStorage(etag='"dup-hash-1"')
    service = _service(storage)

    class _ExistingRepository:
        """替身：`get_by_file_hash` 命中已有合同（模拟重复上传）。"""

        async def get_by_file_hash(self, file_hash: str):
            del file_hash
            return object()

    monkeypatch.setattr(service, "_contracts", _ExistingRepository())
    body = _upload_body("spec.docx")

    error = _rejected(lambda: asyncio.run(service.upload(body)))

    assert error.status_code == 409
    assert "ERR_FILE_ALREADY_UPLOADED" in error.detail
    # 已通过对象存储在确（说明拒绝发生在写库阶段，而不是更早的参数校验）
    assert storage.stat_calls == [body.file_key]
