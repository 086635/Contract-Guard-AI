"""C57 上传内容扫描任务测试（离线）。

覆盖 `4-07 §⑦` 四条断言 + §⑧ 四个用例名：

1. 伪造扩展名拒绝（magic/结构嗅探，不看扩展名）
2. EICAR/测试签名触发拒绝（ClamAV 端口用 Fake，不依赖真实 clamd）
3. AV 超时拒绝（超时按失败处理）
4. PDF/DOCX 合规文件通过
5. `test_scan_none_parse`：扫描不通过 → **不派发 parse**

全部用例不依赖 PostgreSQL / MinIO / 真实 ClamAV。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from worker.storage import av_scan
from worker.storage.av_scan import (
    FAILED,
    REASON_AV_INFECTED,
    REASON_AV_TIMEOUT,
    REASON_AV_UNAVAILABLE,
    REASON_MAGIC_MISMATCH,
    REASON_TOO_LARGE,
    SUCCESS,
    ClamdScanner,
    detect_document_type,
)
from worker.tasks import scan_upload as scan_module
from worker.tasks.scan_upload import MAX_SCAN_BYTES, scan_bytes, scan_upload_task

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "packages" / "doc-processor" / "tests" / "fixtures"

#: EICAR 标准测试串（**无害测试签名**，按片段拼接以免静态扫描器误报本文件）。
_EICAR = b"".join(
    [
        b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR",
        b"-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*",
    ]
)


class _FakeClamdSocket:
    """`clamd` TCP 应答替身：只按脚本回一次，再返回 EOF。"""

    def __init__(self, response: bytes) -> None:
        self._response = response
        self.sent: list[bytes] = []
        self._drained = False

    def __enter__(self) -> _FakeClamdSocket:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def settimeout(self, value: float) -> None:
        del value

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        del size
        if self._drained:
            return b""
        self._drained = True
        return self._response


def _forged_pdf() -> bytes:
    """伪装成 PDF 的可执行头（扩展名/声明类型都不足以通过内容判据）。"""
    return b"MZ\x90\x00" + b"\x00" * 256


def _run(
    file_key: str,
    file_name: str,
    file_type: str,
    contract_id: str | None = None,
    local_path: Path | str | None = None,
) -> dict:
    kwargs = {} if local_path is None else {"local_path": str(local_path)}
    args = (
        (file_key, file_name, file_type)
        if contract_id is None
        else (
            file_key,
            file_name,
            file_type,
            contract_id,
        )
    )
    return scan_upload_task.apply(args=args, kwargs=kwargs).get()


@pytest.fixture(autouse=True)
def _av_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认回到离线档（`CG_UPLOAD_AV_MODE` 未设置即 off）。"""
    monkeypatch.delenv("CG_UPLOAD_AV_MODE", raising=False)


# --------------------------------------------------------------------------
# 1. magic / 结构嗅探（纯 Python，离线）
# --------------------------------------------------------------------------


def test_detect_reads_content_not_extension() -> None:
    """同一份 DOCX 内容：叫 .pdf 也识别为 DOCX；叫 .docx 也识别为 DOCX。"""
    docx = (_FIXTURES / "contract.docx").read_bytes()
    assert detect_document_type(docx) == "DOCX"

    pdf = (_FIXTURES / "native.pdf").read_bytes()
    assert detect_document_type(pdf) == "PDF"

    assert detect_document_type(b"MZ\x90\x00" + b"\x00" * 512) is None


def test_magic_mismatch_rejected() -> None:
    """伪造扩展名：`.pdf` 文件名 + 非 PDF 内容 → FAILED（MAGIC_MISMATCH）。"""
    forged = b"PK\x03\x04" + b"not-a-real-zip" * 8
    result = scan_bytes(forged, "PDF")

    assert result.status == FAILED
    assert result.reason == REASON_MAGIC_MISMATCH
    assert result.safe_to_parse is False


def test_docx_requires_real_word_parts() -> None:
    """`.zip`/`.xlsx` 改成 `.docx`：ZIP 容器但缺 `word/document.xml` → 拒绝。"""
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile

    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    result = scan_bytes(buffer.getvalue(), "DOCX")

    assert result.status == FAILED
    assert result.reason == REASON_MAGIC_MISMATCH


def test_compliant_pdf_and_docx_pass() -> None:
    """合规 PDF / DOCX 内容 → SUCCESS（离线档不调 ClamAV）。"""
    pdf = scan_bytes((_FIXTURES / "native.pdf").read_bytes(), "PDF")
    docx = scan_bytes((_FIXTURES / "contract.docx").read_bytes(), "DOCX")

    assert (pdf.status, pdf.detected_type, pdf.reason) == (SUCCESS, "PDF", None)
    assert (docx.status, docx.detected_type, docx.reason) == (SUCCESS, "DOCX", None)


def test_oversized_content_rejected() -> None:
    """超过读取上限的内容直接阻断（防御性上限，与网关 50 MB 同口径）。"""
    result = scan_bytes(b"%PDF-" + b"0" * (MAX_SCAN_BYTES + 1), "PDF")
    assert result.status == FAILED
    assert result.reason == REASON_TOO_LARGE


# --------------------------------------------------------------------------
# 2/3. ClamAV：感染与超时（端口 Fake，不依赖真实 clamd）
# --------------------------------------------------------------------------


def test_clamav_infected_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """EICAR 测试签名 → FAILED(AV_INFECTED)，且只返回签名名、不回显内容。"""
    monkeypatch.setattr(
        av_scan.socket,
        "create_connection",
        lambda *args, **kwargs: _FakeClamdSocket(
            b"stream: Eicar-Test-Signature FOUND\x00"
        ),
    )

    result = ClamdScanner(host="clamav", port=3310).scan(_EICAR)

    assert result.status == FAILED
    assert result.reason is not None
    assert result.reason.startswith(REASON_AV_INFECTED)
    assert "Eicar-Test-Signature" in result.reason
    assert "X5O!P%" not in result.reason  # 不回显文件内容


def test_clamav_clean_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """clamd 返回 OK → SUCCESS。"""
    monkeypatch.setattr(
        av_scan.socket,
        "create_connection",
        lambda *args, **kwargs: _FakeClamdSocket(b"stream: OK\x00"),
    )

    result = ClamdScanner().scan(b"%PDF-1.7\n%%EOF\n")

    assert result.status == SUCCESS
    assert result.reason is None


def test_av_timeout_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """clamd 超时 → FAILED(AV_TIMEOUT)：**超时按失败处理**，不得静默放行。"""

    def _timeout(*args: object, **kwargs: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(av_scan.socket, "create_connection", _timeout)

    result = ClamdScanner(timeout_s=0.01).scan(b"%PDF-1.7\n%%EOF\n")

    assert result.status == FAILED
    assert result.reason == REASON_AV_TIMEOUT
    assert result.safe_to_parse is False


def test_av_unavailable_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """clamd 连不上 → FAILED(AV_UNAVAILABLE)：fail closed，不是 fail open。"""

    def _refused(*args: object, **kwargs: object) -> None:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(av_scan.socket, "create_connection", _refused)

    result = ClamdScanner().scan(b"%PDF-1.7\n%%EOF\n")

    assert result.status == FAILED
    assert result.reason is not None
    assert result.reason.startswith(REASON_AV_UNAVAILABLE)


# --------------------------------------------------------------------------
# 4. 任务级：签名、chaining（扫描不通过 → 不派发 parse）
# --------------------------------------------------------------------------


def test_scan_none_parse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """扫描不通过绝不派发 parse；通过才派发，并在返回值里带上 parseTaskId。"""
    dispatched: list[tuple[str, str]] = []

    def _fake_dispatch(file_key: str, contract_id: str) -> str:
        dispatched.append((file_key, contract_id))
        return "parse-task-1"

    monkeypatch.setattr(scan_module, "dispatch_parse", _fake_dispatch)

    forged = tmp_path / "forged.pdf"
    forged.write_bytes(b"MZ\x90\x00" + b"\x00" * 256)  # 伪装成 .pdf 的可执行头
    rejected = _run("key", "forged.pdf", "PDF_NATIVE", "c-reject", forged)
    assert rejected["status"] == FAILED
    assert rejected["reason"] == REASON_MAGIC_MISMATCH
    assert dispatched == []
    assert rejected.get("parseTaskId") is None

    clean = _run("key", "native.pdf", "PDF_NATIVE", "c-clean", _FIXTURES / "native.pdf")
    assert clean["status"] == SUCCESS
    assert dispatched == [("key", "c-clean")]
    assert clean["parseTaskId"] == "parse-task-1"


def test_scan_task_docx_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """DOCX 合规内容走同一链路：SUCCESS → 派发 parse。"""
    monkeypatch.setattr(
        scan_module, "dispatch_parse", lambda file_key, contract_id: "parse-docx-1"
    )

    payload = _run(
        "key", "contract.docx", "DOCX", "c-docx", _FIXTURES / "contract.docx"
    )

    assert payload["status"] == SUCCESS
    assert payload["detectedType"] == "DOCX"
    assert payload["parseTaskId"] == "parse-docx-1"


def test_scan_task_clamav_infected_does_not_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务级：`CG_UPLOAD_AV_MODE=clamav` + 感染结论 → 拒绝且不派发 parse。"""
    monkeypatch.setenv("CG_UPLOAD_AV_MODE", "clamav")
    monkeypatch.setattr(
        scan_module,
        "_build_scanner",
        lambda: _StubScanner(),
    )
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scan_module,
        "dispatch_parse",
        lambda file_key, contract_id: dispatched.append((file_key, contract_id)) or "x",
    )

    payload = _run(
        "key", "native.pdf", "PDF_NATIVE", "c-virus", _FIXTURES / "native.pdf"
    )

    assert payload["status"] == FAILED
    assert payload["reason"] == REASON_AV_INFECTED
    assert dispatched == []
    assert payload.get("parseTaskId") is None


class _StubScanner:
    """返回固定 INFECTED 结论的扫描端口替身。"""

    def scan(self, data: bytes):
        del data
        from worker.storage.av_scan import ScanResult

        return ScanResult(FAILED, None, REASON_AV_INFECTED)


def test_scan_task_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿不到源文件 → 返回 FAILED 信封（**不抛异常给 broker**）。"""

    class _Broken:
        def get_object(self, bucket: str, key: str) -> object:
            raise OSError("minio down")

    monkeypatch.setattr(scan_module, "get_minio_client", lambda: _Broken())

    payload = _run("no-such.pdf", "no-such.pdf", "PDF_NATIVE")

    assert payload["status"] == FAILED
    assert str(payload["reason"]).startswith("SOURCE_UNAVAILABLE")
    assert payload.get("parseTaskId") is None


def test_scan_task_rejects_unknown_file_type() -> None:
    """未登记的声明类型 → FAILED(ERR_UNSUPPORTED_FILE_TYPE)。

    ⚠️ T-D8-2 起 `IMAGE` **已是登记类型**（见下方图片用例），故这里改用仍未登记的类型。
    """
    payload = _run("key", "x.xlsx", "XLSX", _FIXTURES / "native.pdf")

    assert payload["status"] == FAILED
    assert payload["reason"] == "ERR_UNSUPPORTED_FILE_TYPE"


def test_payload_status_reuses_platform_task_vocabulary() -> None:
    """拒绝必须用平台任务状态词表 `FAILED`（回到字面量断言）。

    网关 `TaskService.get` 的既有约定是 `result.status == "FAILED"` → `Status.failed`；
    若这里改成自定义词（如 `REJECTED`），被拒的上传会在前端显示成「完成」。
    """
    rejected = _run("key", "forged.pdf", "PDF_NATIVE", None, _forged_pdf())
    accepted = _run("key", "native.pdf", "PDF_NATIVE", None, _FIXTURES / "native.pdf")

    assert rejected["status"] == "FAILED"
    assert accepted["status"] == "SUCCESS"


def test_task_signature_keeps_card_contract() -> None:
    """签名稳定：C57 §⑤ 的前三个业务参数顺序即跨进程约定。"""
    import inspect

    parameters = list(inspect.signature(scan_upload_task.run).parameters)
    assert parameters[:3] == ["file_key", "file_name", "file_type"]
    assert "contract_id" in parameters
    assert "local_path" in parameters


def test_scan_task_is_registered_in_celery_include() -> None:
    """`worker.main` 必须注册扫描任务，否则 worker 启动后收不到。"""
    from worker.main import celery_app

    assert "worker.tasks.scan_upload" in celery_app.conf.include


# ---------------------------------------------------------------------------
# T-D8-2 / C77：图片输入（PNG / JPEG / TIFF）按**内容**识别
# ---------------------------------------------------------------------------
def _png() -> bytes:
    """最小合法 PNG 头：8 字节签名 + 长度 + IHDR。"""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\r" + b"IHDR" + b"\x00" * 32


def _jpeg() -> bytes:
    """最小合法 JPEG：SOI + APP0 + ... + EOI。"""
    return b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _tiff() -> bytes:
    """最小合法 TIFF：little-endian 魔数 II*\0。"""
    return b"II*\x00" + b"\x00" * 64


@pytest.mark.parametrize("payload", [_png(), _jpeg(), _tiff()])
def test_image_magic_detected(payload: bytes) -> None:
    """PNG / JPEG / TIFF 内容被识别为 IMAGE（不看扩展名）。"""
    assert detect_document_type(payload) == "IMAGE"


@pytest.mark.parametrize("payload", [_png(), _jpeg(), _tiff()])
def test_image_scan_bytes_passes_for_image_type(payload: bytes) -> None:
    """声明 IMAGE 且内容判据一致时，`scan_bytes` 放行（AV off 档）。"""
    result = scan_bytes(payload, "IMAGE", mode="off")
    assert result.status == SUCCESS
    assert result.detected_type == "IMAGE"


def test_image_magic_is_structurally_checked() -> None:
    """只伪造魔数、结构不完整必须被拒（不能只认前几个字节）。"""
    # PNG 魔数但不含 IHDR
    assert detect_document_type(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) is None
    # JPEG SOI 但没有 EOI 收尾
    assert detect_document_type(b"\xff\xd8\xff" + b"\x00" * 32) is None


def test_renamed_binary_is_not_image() -> None:
    """可执行头改名成 .png 仍被内容判据拒绝。"""
    assert detect_document_type(_forged_pdf()) is None
    result = scan_bytes(_forged_pdf(), "IMAGE", mode="off")
    assert result.status == FAILED
    assert result.reason == REASON_MAGIC_MISMATCH


def test_scan_upload_accepts_image_declared_type() -> None:
    """`scan_upload_task` 的类型白名单已登记 IMAGE（否则会 ERR_UNSUPPORTED_FILE_TYPE）。"""
    assert scan_module._EXPECTED_TYPE_BY_FILE_TYPE.get("IMAGE") == "IMAGE"
