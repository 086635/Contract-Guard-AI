"""上传内容扫描（magic sniff + ClamAV）。对应 C57 / `4-07 §⑤.8`。

设计要点：
1. **纯 Python** magic/结构嗅探，可离线测试；扩展名只作声明一致性，**不是判据**。
2. ClamAV 走 `clamd` INSTREAM 协议（stdlib socket，无新增依赖），可注入替身；
   超时/不可用一律按**失败**处理（阻断解析），不静默放行。
3. 结论里的 `reason` 只放**安全摘要**：不写文件内容，也不回显用户文件名。
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from io import BytesIO
from zipfile import BadZipFile, ZipFile

#: 扫描结论（唯一取值集合）：仅 `CLEAN` 允许继续解析。
CLEAN = "CLEAN"
REJECTED = "REJECTED"

#: 拒绝/失败的安全摘要码（`ScanResult.reason`）。
REASON_MAGIC_MISMATCH = "MAGIC_MISMATCH"
REASON_UNSUPPORTED_TYPE = "ERR_UNSUPPORTED_FILE_TYPE"
REASON_AV_INFECTED = "AV_INFECTED"
REASON_AV_TIMEOUT = "AV_TIMEOUT"
REASON_AV_UNAVAILABLE = "AV_UNAVAILABLE"
REASON_AV_BAD_RESPONSE = "AV_BAD_RESPONSE"
REASON_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
REASON_DISPATCH_FAILED = "DISPATCH_FAILED"
REASON_TOO_LARGE = "ERR_FILE_TOO_LARGE"

PDF_MAGIC = b"%PDF-"
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
DOCX_CONTENT_TYPES = "[Content_Types].xml"
DOCX_MAIN_PART = "word/document.xml"

#: PDF 尾部标记：真实 PDF 以 `%%EOF` 收尾（允许尾随空白）。
_PDF_TAIL_WINDOW = 2048


@dataclass(frozen=True, slots=True)
class ScanResult:
    """扫描结论（C57 §⑤ `ScanResult{status,detectedType,reason}`）。"""

    status: str
    detected_type: str | None = None
    reason: str | None = None

    @property
    def safe_to_parse(self) -> bool:
        """仅 `CLEAN` 允许派发解析。"""
        return self.status == CLEAN

    def to_payload(self) -> dict[str, object]:
        """转成任务返回值（camelCase，与契约字段名一致）。"""
        return {
            "status": self.status,
            "detectedType": self.detected_type,
            "reason": self.reason,
        }


def detect_document_type(data: bytes) -> str | None:
    """按**文件内容**判定类型：`PDF` / `DOCX` / `None`（无法识别）。

    只看字节，不看扩展名——这是「不把扩展名当 magic」的落点。
    """
    if not data:
        return None
    if data.startswith(PDF_MAGIC):
        return "PDF" if PDF_MAGIC and _pdf_is_closed(data) else None
    if data[:4] in ZIP_MAGICS and _looks_like_docx(data):
        return "DOCX"
    return None


def _pdf_is_closed(data: bytes) -> bool:
    """PDF 结构校验：尾部窗口内必须出现 `%%EOF`。"""
    return b"%%EOF" in data[-_PDF_TAIL_WINDOW:]


def _looks_like_docx(data: bytes) -> bool:
    """DOCX 结构校验：ZIP 容器内必须有 `[Content_Types].xml` 与主文档部件。

    `.zip` 改名成 `.docx`、或把 `.xlsx` 当 DOCX 提交，都会在这里被拒。
    """
    try:
        with ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (BadZipFile, OSError, ValueError):
        return False
    return DOCX_CONTENT_TYPES in names and DOCX_MAIN_PART in names


def av_mode() -> str:
    """读取 `CG_UPLOAD_AV_MODE`（`off|clamav`，默认 `off`）。"""
    return os.getenv("CG_UPLOAD_AV_MODE", "off").strip().lower()


class ClamdScanner:
    """`clamd` INSTREAM 客户端（stdlib socket，无新增依赖）。

    Args:
        host: clamd 主机；compose 内为服务名 `clamav`。
        port: clamd TCP 端口，默认 3310。
        timeout_s: 连接与收发超时；超时按**失败**处理（阻断解析）。
    """

    def __init__(
        self, host: str = "clamav", port: int = 3310, *, timeout_s: float = 15.0
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout_s

    def scan(self, data: bytes) -> ScanResult:
        """扫描字节流；**不抛异常**，任何异常都折算成 `REJECTED` 结论。"""
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=self._timeout
            ) as sock:
                sock.settimeout(self._timeout)
                sock.sendall(b"zINSTREAM\x00")
                for chunk in _chunks(data, 65536):
                    sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
                sock.sendall(b"\x00\x00\x00\x00")
                verdict = _read_until_nul(sock)
        except TimeoutError:
            return ScanResult(REJECTED, None, REASON_AV_TIMEOUT)
        except OSError as exc:
            return ScanResult(
                REJECTED, None, f"{REASON_AV_UNAVAILABLE}:{type(exc).__name__}"
            )

        return _verdict_to_result(verdict)


def _verdict_to_result(verdict: bytes) -> ScanResult:
    """把 clamd 文本应答折算成结论（只取签名名，不回显输入内容）。"""
    text = verdict.decode("utf-8", "replace").replace("\x00", "").strip()
    lowered = text.lower()
    if lowered.endswith("ok"):
        return ScanResult(CLEAN, None, None)
    if "found" in lowered:
        signature = text.split(":", 1)[1].strip() if ":" in text else text
        return ScanResult(REJECTED, None, f"{REASON_AV_INFECTED}:{signature}")
    return ScanResult(REJECTED, None, REASON_AV_BAD_RESPONSE)


def _chunks(data: bytes, size: int) -> list[bytes]:
    return [data[index : index + size] for index in range(0, len(data), size)]


def _read_until_nul(sock: socket.socket, *, limit: int = 4096) -> bytes:
    buffer = bytearray()
    while len(buffer) < limit:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\x00" in chunk:
            break
    return bytes(buffer)


__all__ = [
    "CLEAN",
    "REASON_AV_BAD_RESPONSE",
    "REASON_AV_INFECTED",
    "REASON_AV_TIMEOUT",
    "REASON_AV_UNAVAILABLE",
    "REASON_DISPATCH_FAILED",
    "REASON_MAGIC_MISMATCH",
    "REASON_SOURCE_UNAVAILABLE",
    "REASON_TOO_LARGE",
    "REASON_UNSUPPORTED_TYPE",
    "REJECTED",
    "ClamdScanner",
    "ScanResult",
    "av_mode",
    "detect_document_type",
]
