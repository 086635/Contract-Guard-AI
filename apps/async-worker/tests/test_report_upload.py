"""MinIO 报告上传离线单测（C19）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from minio import Minio

from worker.storage import minio_client as minio_module
from worker.storage import upload_report

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MINIO_TEST_ENDPOINT = os.getenv("CG_MINIO_TEST_ENDPOINT")


class _FakeMinio:
    """记录 put_object 调用的最小假客户端。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes, int, str]] = []

    def put_object(
        self,
        bucket: str,
        key: str,
        data: Any,
        length: int,
        content_type: str,
    ) -> None:
        self.calls.append((bucket, key, data.read(), length, content_type))


def test_upload_report_uses_contracts_bucket_and_docx_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CG_MINIO_BUCKET", raising=False)
    fake = _FakeMinio()
    monkeypatch.setattr(minio_module, "get_minio_client", lambda: fake)

    data = b"PK\x03\x04report-bytes"
    key = upload_report("contract-19", data)

    assert re.fullmatch(
        r"reports/contract-19/\d{14}_[0-9a-f]{8}\.docx",
        key,
    )
    assert fake.calls == [("contracts", key, data, len(data), _DOCX_MIME)]


def test_upload_report_honors_bucket_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeMinio()
    monkeypatch.setenv("CG_MINIO_BUCKET", "reports-test")
    monkeypatch.setattr(minio_module, "get_minio_client", lambda: fake)

    key = upload_report("contract-19", b"data")

    assert fake.calls[0][0] == "reports-test"
    assert fake.calls[0][1] == key


def test_upload_report_rejects_empty_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        minio_module,
        "get_minio_client",
        lambda: pytest.fail("空内容不得构造 MinIO 客户端"),
    )

    with pytest.raises(ValueError, match="不能为空"):
        upload_report("contract-19", b"")


@pytest.mark.parametrize(
    "contract_id",
    ["", "a/b", "a\\b", "..", "a..b"],
)
def test_upload_report_rejects_invalid_contract_id(
    contract_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        minio_module,
        "get_minio_client",
        lambda: pytest.fail("非法 ID 不得构造 MinIO 客户端"),
    )

    with pytest.raises(ValueError, match="contract_id"):
        upload_report(contract_id, b"data")


def test_minio_client_honors_secret_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    access_file = tmp_path / "access_key"
    secret_file = tmp_path / "secret_key"
    access_file.write_text("file-access\n", encoding="utf-8")
    secret_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("CG_MINIO_ACCESS_KEY", "env-access")
    monkeypatch.setenv("CG_MINIO_SECRET_KEY", "env-secret")
    monkeypatch.setenv("CG_MINIO_ACCESS_KEY_FILE", str(access_file))
    monkeypatch.setenv("CG_MINIO_SECRET_KEY_FILE", str(secret_file))
    captured: dict[str, object] = {}

    def fake_minio(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(minio_module, "Minio", fake_minio)
    minio_module.get_minio_client()

    assert captured["access_key"] == "file-access"
    assert captured["secret_key"] == "file-secret"


def test_minio_client_rejects_empty_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("CG_MINIO_ACCESS_KEY_FILE", str(empty))

    with pytest.raises(RuntimeError, match="CG_MINIO_ACCESS_KEY_FILE 为空"):
        minio_module.get_minio_client()


def test_storage_package_exports_upload_report() -> None:
    assert upload_report is minio_module.upload_report


def test_minio_client_has_no_forbidden_imports() -> None:
    source = Path(minio_module.__file__).read_text(encoding="utf-8")

    assert "from app." not in source
    assert "import app." not in source
    assert "sqlalchemy" not in source


@pytest.mark.skipif(
    not _MINIO_TEST_ENDPOINT,
    reason="未设置 CG_MINIO_TEST_ENDPOINT，跳过真实 MinIO 报告上传测试",
)
def test_upload_report_real_minio_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _MINIO_TEST_ENDPOINT is not None
    bucket = os.getenv("CG_MINIO_TEST_BUCKET", "contracts")
    access_key = os.getenv("CG_MINIO_TEST_ACCESS_KEY", "minioadmin")
    secret_key = os.getenv("CG_MINIO_TEST_SECRET_KEY", "minioadmin")
    client = Minio(
        _MINIO_TEST_ENDPOINT,
        access_key=access_key,
        secret_key=secret_key,
        secure=False,
    )

    try:
        bucket_exists = client.bucket_exists(bucket)
    except Exception as exc:  # noqa: BLE001 - 外部服务不可达时跳过集成用例
        pytest.skip(f"MinIO 不可达：{exc}")
    if not bucket_exists:
        pytest.skip(f"MinIO bucket 不存在：{bucket}")

    monkeypatch.setenv("CG_MINIO_ENDPOINT", _MINIO_TEST_ENDPOINT)
    monkeypatch.setenv("CG_MINIO_BUCKET", bucket)
    monkeypatch.setenv("CG_MINIO_ACCESS_KEY", access_key)
    monkeypatch.setenv("CG_MINIO_SECRET_KEY", secret_key)

    data = b"PK\x03\x04real-minio-report"
    key = upload_report("contract-19-real", data)
    try:
        response = client.get_object(bucket, key)
        try:
            assert response.read() == data
        finally:
            response.close()
            response.release_conn()
    finally:
        client.remove_object(bucket, key)
