"""C59 DOCX→PDF 转换任务测试（离线）。

`subprocess` 与 MinIO 全部用替身，因此**不依赖 LibreOffice / PG / MinIO**：
用假 `soffice` 写入产物模拟成功、用非 0 返回码模拟失败、用 `TimeoutExpired` 模拟超时。

真实 LibreOffice 集成用例由 `CG_CONVERT_TEST_SOFFICE` 门禁（未设置则 skip）。
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest

from worker.main import CONVERT_QUEUE, celery_app
from worker.tasks import convert_doc as convert_module
from worker.tasks.convert_doc import (
    FAILED,
    PREVIEW_KEY_PREFIX,
    SUCCESS,
    convert_doc_task,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "packages" / "doc-processor" / "tests" / "fixtures"
_FAKE_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


class FakeMinio:
    """MinIO 替身：记录读写，**故意提供 remove_object 以便断言从不删除原文**。"""

    def __init__(self) -> None:
        self.fget_calls: list[tuple[str, str, str]] = []
        self.put_calls: list[dict[str, object]] = []
        self.remove_calls: list[tuple[str, str]] = []

    def fget_object(self, bucket: str, key: str, file_path: str) -> str:
        self.fget_calls.append((bucket, key, file_path))
        Path(file_path).write_bytes(b"PK\x03\x04-fake-docx-body")
        return file_path

    def put_object(
        self,
        bucket: str,
        key: str,
        data: object,
        *,
        length: int | None = None,
        content_type: str | None = None,
    ) -> None:
        payload = data.read() if hasattr(data, "read") else data
        self.put_calls.append(
            {
                "bucket": bucket,
                "key": key,
                "data": payload,
                "length": length,
                "content_type": content_type,
            }
        )

    def remove_object(self, bucket: str, key: str) -> None:
        self.remove_calls.append((bucket, key))


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定转换器版本（免探测）并让结果后端为空（免 redis 重试）。"""
    monkeypatch.setenv("CG_CONVERTER_VERSION", "libreoffice-test-1.0")
    monkeypatch.setattr(
        convert_doc_task.app.conf, "result_backend", None, raising=False
    )


def _fake_soffice(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    raises: BaseException | None = None,
    out_bytes: bytes = _FAKE_PDF,
) -> list[list[str]]:
    """替换 `subprocess.run`：成功时按 LibreOffice 约定写出 `<stem>.pdf`。"""
    argv_seen: list[list[str]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        argv_seen.append(list(argv))
        if raises is not None:
            raise raises
        if returncode == 0:
            out_dir = Path(argv[argv.index("--outdir") + 1])
            source = Path(argv[-1])
            (out_dir / f"{source.stem}.pdf").write_bytes(out_bytes)
        return subprocess.CompletedProcess(argv, returncode, "", "boom")

    monkeypatch.setattr(convert_module.subprocess, "run", _run)
    return argv_seen


def _run(
    contract_id: str = "c-1",
    source_key: str = "contracts/2026/10/abc-采购合同.docx",
    *,
    local_path: Path | str | None = None,
) -> dict:
    kwargs = {} if local_path is None else {"local_path": str(local_path)}
    return convert_doc_task.apply(
        args=(contract_id, source_key),
        kwargs=kwargs,
    ).get()


# --------------------------------------------------------------------------
# 1. 断言 1：DOCX 成功生成 PDF
# --------------------------------------------------------------------------
def test_convert_doc_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功路径：产物上传到服务端生成的派生 key，返回 storagePath/converterVersion。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    argv_seen = _fake_soffice(monkeypatch)

    payload = _run("contract-1")

    assert payload["status"] == SUCCESS
    assert payload["contractId"] == "contract-1"
    assert payload["warnings"] == []
    assert payload["converterVersion"] == "libreoffice-test-1.0"

    storage_path = payload["storagePath"]
    assert isinstance(storage_path, str)
    assert storage_path.startswith(f"{PREVIEW_KEY_PREFIX}/contract-1/")
    assert storage_path.endswith(".pdf")

    assert len(fake_minio.put_calls) == 1
    uploaded = fake_minio.put_calls[0]
    assert uploaded["key"] == storage_path
    assert uploaded["data"] == _FAKE_PDF
    assert uploaded["length"] == len(_FAKE_PDF)
    assert uploaded["content_type"] == "application/pdf"

    # headless + 明确输出目录 + 源文件路径（不就地转换）
    argv = argv_seen[0]
    assert "--headless" in argv and "--convert-to" in argv
    assert argv[argv.index("--convert-to") + 1] == "pdf"
    out_dir = Path(argv[argv.index("--outdir") + 1])
    assert out_dir.name.startswith("cg-convert-contract-1-")
    assert Path(argv[-1]).parent == out_dir


def test_convert_doc_uses_independent_temp_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """每次转换使用独立临时目录，且任务结束后目录被清理（§⑨-1）。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    argv_seen = _fake_soffice(monkeypatch)

    _run("contract-a", local_path=_FIXTURES / "contract.docx")
    _run("contract-b", local_path=_FIXTURES / "contract.docx")

    dirs = [Path(argv[argv.index("--outdir") + 1]) for argv in argv_seen]
    assert dirs[0] != dirs[1]
    assert all(not directory.exists() for directory in dirs)


# --------------------------------------------------------------------------
# 2. 断言 2：损坏 DOCX FAILED
# --------------------------------------------------------------------------
def test_convert_doc_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """LibreOffice 非 0 退出 → 结构化 FAILED，不上传、不抛异常给 broker。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    _fake_soffice(monkeypatch, returncode=1)

    payload = _run("contract-broken")

    assert payload["status"] == FAILED
    assert payload["storagePath"] is None
    assert payload["converterVersion"] is None
    assert payload["warnings"] == ["CONVERT_EXIT_CODE:1"]
    assert fake_minio.put_calls == []


def test_convert_doc_missing_output_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """退出码 0 但没产出 PDF（例如损坏 DOCX 被吞掉）→ FAILED(CONVERT_NO_OUTPUT)。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)

    def _run_no_output(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(convert_module.subprocess, "run", _run_no_output)

    payload = _run("contract-no-output")

    assert payload["status"] == FAILED
    assert payload["warnings"] == ["CONVERT_NO_OUTPUT"]
    assert fake_minio.put_calls == []


# --------------------------------------------------------------------------
# 3. 断言 3：转换超时 FAILED
# --------------------------------------------------------------------------
def test_convert_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时（TimeoutExpired）→ 结构化 FAILED(CONVERT_TIMEOUT)，不上传。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    _fake_soffice(
        monkeypatch,
        raises=subprocess.TimeoutExpired(cmd="soffice", timeout=0.01),
    )

    payload = _run("contract-slow")

    assert payload["status"] == FAILED
    assert payload["warnings"] == ["CONVERT_TIMEOUT"]
    assert fake_minio.put_calls == []


def test_convert_timeout_is_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """超时值来自 `CG_CONVERT_TIMEOUT_S` 并原样传给子进程。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    monkeypatch.setenv("CG_CONVERT_TIMEOUT_S", "7.5")
    seen: list[float] = []

    def _record_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(float(kwargs["timeout"]))  # type: ignore[arg-type]
        out_dir = Path(argv[argv.index("--outdir") + 1])
        source = Path(argv[-1])
        (out_dir / f"{source.stem}.pdf").write_bytes(_FAKE_PDF)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(convert_module.subprocess, "run", _record_run)

    _run("contract-timeout-env")

    assert seen == [7.5]


# --------------------------------------------------------------------------
# 4. 断言 4：原文仍存在
# --------------------------------------------------------------------------
def test_convert_preserves_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """转换只读原文：本地源文件字节不变、MinIO 源对象不被删除。"""
    source_fixture = _FIXTURES / "contract.docx"
    original = source_fixture.read_bytes()

    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    _fake_soffice(monkeypatch)

    payload = _run("contract-keep", local_path=source_fixture)

    assert payload["status"] == SUCCESS
    assert source_fixture.is_file()
    assert source_fixture.read_bytes() == original
    assert fake_minio.remove_calls == []


def test_convert_never_deletes_or_overwrites_source_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """走 MinIO 源对象时：只 fget 读取，不 remove/不覆盖原 key。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    _fake_soffice(monkeypatch)

    source_key = "contracts/2026/10/xyz-采购合同.docx"
    payload = _run("contract-keep-2", source_key=source_key)

    assert payload["status"] == SUCCESS
    assert [call[1] for call in fake_minio.fget_calls] == [source_key]
    assert fake_minio.remove_calls == []
    assert payload["storagePath"] != source_key
    assert all(call["key"] != source_key for call in fake_minio.put_calls)


# --------------------------------------------------------------------------
# 5. 断言 5：重试不覆盖其他合同
# --------------------------------------------------------------------------
def test_retry_does_not_overwrite_other_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """派生对象键按合同命名空间 + 每次唯一：重试与跨合同都不覆盖。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    _fake_soffice(monkeypatch)

    first = _run("contract-x")["storagePath"]
    retry = _run("contract-x")["storagePath"]
    other = _run("contract-y")["storagePath"]

    assert first != retry != other
    assert first.startswith(f"{PREVIEW_KEY_PREFIX}/contract-x/")
    assert retry.startswith(f"{PREVIEW_KEY_PREFIX}/contract-x/")
    assert other.startswith(f"{PREVIEW_KEY_PREFIX}/contract-y/")


def test_missing_local_source_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源文件不存在 → 结构化 FAILED（不抛异常给 broker）。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)

    payload = _run("contract-missing", local_path=_FIXTURES / "no-such.docx")

    assert payload["status"] == FAILED
    assert payload["warnings"] == ["CONVERT_UNAVAILABLE:FileNotFoundError"]


# --------------------------------------------------------------------------
# 6. 契约与队列（跨进程约定）
# --------------------------------------------------------------------------
def test_task_signature_matches_card_interface() -> None:
    """签名即 C58→C59 跨进程约定：`convert_doc(contract_id, source_key)`。"""
    parameters = list(inspect.signature(convert_doc_task.run).parameters)
    assert parameters[:2] == ["contract_id", "source_key"]
    assert "local_path" in parameters


def test_convert_task_routed_to_convert_queue() -> None:
    """转换任务必须登记到 `convert` 独立队列（`CELERY_QUEUES=convert`）。"""
    assert CONVERT_QUEUE == "convert"
    assert celery_app.conf.task_routes["convert_doc"]["queue"] == CONVERT_QUEUE
    assert "worker.tasks.convert_doc" in celery_app.conf.include


def test_payload_status_uses_platform_task_vocabulary() -> None:
    """结果状态用平台词表 `SUCCESS`/`FAILED`（网关据此判失败）。"""
    assert (SUCCESS, FAILED) == ("SUCCESS", "FAILED")


# --------------------------------------------------------------------------
# 7. 真实 LibreOffice 集成（默认 skip）
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.getenv("CG_CONVERT_TEST_SOFFICE"),
    reason="未设置 CG_CONVERT_TEST_SOFFICE，跳过真实 LibreOffice 转换集成测试",
)
def test_convert_doc_with_real_libreoffice(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 `soffice` 把夹具 DOCX 转出非空 PDF（需容器/本机装 LibreOffice）。"""
    fake_minio = FakeMinio()
    monkeypatch.setattr(convert_module, "get_minio_client", lambda: fake_minio)
    monkeypatch.delenv("CG_CONVERTER_VERSION", raising=False)

    payload = _run("contract-real", local_path=_FIXTURES / "contract.docx")

    assert payload["status"] == SUCCESS, payload
    assert payload["storagePath"].endswith(".pdf")
    uploaded = fake_minio.put_calls[0]["data"]
    assert isinstance(uploaded, bytes) and uploaded.startswith(b"%PDF-")
