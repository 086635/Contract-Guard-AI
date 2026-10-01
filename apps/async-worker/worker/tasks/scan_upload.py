"""上传内容扫描任务（C57 / `4-07 §⑤.8` §⑥.8）。

流程：读取对象字节 → **magic/结构嗅探** → （`CG_UPLOAD_AV_MODE=clamav` 时）ClamAV 扫描
→ 返回 `ScanResult` 信封；**扫描未通过绝不派发 `parse_doc`**。

接口（§⑤）：`scan_upload(file_key, file_name, file_type) -> ScanResult{status,detectedType,reason}`
—— 本实现额外接受 `contract_id`（扫描通过后链式派发解析所需，网关必传）与
`local_path`（仅离线联调/测试，与 `parse_doc` 同款）。

🔴 三条红线：扩展名不是判据（§⑨-1）；扫描不可用/超时按失败处理（§⑥-2）；
日志与返回值只放安全摘要，不写文件内容（§⑥-3/4）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from cg_observability import get_logger, log_event
from worker.main import celery_app
from worker.storage import get_minio_client
from worker.storage.av_scan import (
    FAILED,
    REASON_DISPATCH_FAILED,
    REASON_MAGIC_MISMATCH,
    REASON_SOURCE_UNAVAILABLE,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED_TYPE,
    SUCCESS,
    ClamdScanner,
    ScanResult,
    av_mode,
    detect_document_type,
)

_LOG = get_logger("worker.scan")

#: 读取上限：与 api-gateway `MAX_UPLOAD_BYTES` 同口径（多读 1 字节以识别超限）。
MAX_SCAN_BYTES = 50 * 1024 * 1024

#: 声明文件类型 → 期望的内容类型（**内容判据**由 `detect_document_type` 给出）。
_EXPECTED_TYPE_BY_FILE_TYPE: dict[str, str] = {
    "PDF_NATIVE": "PDF",
    "PDF_SCANNED": "PDF",
    "DOCX": "DOCX",
}


def dispatch_parse(file_key: str, contract_id: str) -> str:
    """扫描通过后派发 `parse_doc`（worker→worker，同一 Celery app）。"""
    return str(celery_app.send_task("parse_doc", args=[file_key, contract_id]).id)


def _read_source_bytes(file_key: str, local_path: str | None) -> bytes:
    """读取对象字节（本地路径优先）；最多读 `MAX_SCAN_BYTES + 1` 字节。"""
    if local_path:
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(local_path)
        return path.read_bytes()[: MAX_SCAN_BYTES + 1]

    bucket = os.getenv("CG_MINIO_BUCKET", "contracts")
    response = get_minio_client().get_object(bucket, file_key)
    try:
        return response.read(MAX_SCAN_BYTES + 1)
    finally:
        response.close()
        response.release_conn()


def _build_scanner() -> ClamdScanner:
    """按环境变量构造 clamd 客户端（compose 内主机名为 `clamav`）。"""
    return ClamdScanner(
        host=os.getenv("CG_CLAMAV_HOST", "clamav"),
        port=int(os.getenv("CG_CLAMAV_PORT", "3310")),
        timeout_s=float(os.getenv("CG_CLAMAV_TIMEOUT_S", "15")),
    )


def scan_bytes(
    data: bytes, expected_type: str, *, mode: str | None = None
) -> ScanResult:
    """对单份内容做「magic/结构校验 + 可选 ClamAV 扫描」（纯函数，可离线测试）。"""
    if len(data) > MAX_SCAN_BYTES:
        return ScanResult(FAILED, None, REASON_TOO_LARGE)

    detected = detect_document_type(data)
    if detected is None or detected != expected_type:
        return ScanResult(FAILED, detected, REASON_MAGIC_MISMATCH)

    if (mode or av_mode()) != "clamav":
        return ScanResult(SUCCESS, detected, None)

    verdict = _build_scanner().scan(data)
    return ScanResult(verdict.status, detected, verdict.reason)


@celery_app.task(name="scan_upload", bind=True)
def scan_upload_task(
    self,
    file_key: str,
    file_name: str,
    file_type: str,
    contract_id: str | None = None,
    *,
    local_path: str | None = None,
) -> dict:
    """扫描单个上传对象（§⑤）。

    Args:
        file_key: MinIO 对象键（或 `local_path` 场景下的占位值）。
        file_name: 原始文件名——**只用于跨进程契约占位，不参与判据**（§⑨-1）。
        file_type: 声明类型（`PDF_NATIVE` / `PDF_SCANNED` / `DOCX`）。
        contract_id: 扫描通过后链式派发 `parse_doc` 所需的合同 ID。
        local_path: 离线联调/测试用的本地路径（仅在给定且存在时生效）。

    Returns:
        `ScanResult` 信封 + （通过且给了 `contract_id` 时）`parseTaskId`；
        `status ∈ {SUCCESS, FAILED}`。**任何情况下都不抛异常给 broker。**
    """
    trace_id = uuid.uuid4().hex
    # 扩展名/文件名不是判据：内容类型由 magic/结构嗅探决定（§⑨-1）。
    del file_name

    expected = _EXPECTED_TYPE_BY_FILE_TYPE.get(str(file_type or "").upper())
    if expected is None:
        result = ScanResult(FAILED, None, REASON_UNSUPPORTED_TYPE)
    else:
        try:
            data = _read_source_bytes(file_key, local_path)
        except Exception as exc:  # noqa: BLE001 - 拿不到内容就没有判据 → 必须阻断
            result = ScanResult(
                FAILED,
                None,
                f"{REASON_SOURCE_UNAVAILABLE}:{type(exc).__name__}",
            )
        else:
            result = scan_bytes(data, expected)

    payload: dict[str, object] = result.to_payload()
    log_event(
        _LOG,
        "scan.result",
        level="INFO" if result.safe_to_parse else "WARNING",
        trace_id=trace_id,
        file_key=file_key,
        file_type=file_type,
        scan_status=result.status,
        reason=result.reason,
    )

    if result.safe_to_parse and contract_id:
        try:
            payload["parseTaskId"] = dispatch_parse(file_key, contract_id)
        except Exception as exc:  # noqa: BLE001 - 派发失败也不能让扫描结论失真
            payload["status"] = FAILED
            payload["reason"] = f"{REASON_DISPATCH_FAILED}:{type(exc).__name__}"
            payload["parseTaskId"] = None
    return payload
