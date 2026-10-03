"""MinIO 对象存储客户端（合同源文件 / 报告导出）。

设计要点：
1. 连接参数统一来自 `CG_MINIO_*`，与 api-gateway 的 MinIO 配置同源。
2. 报告上传只负责生成对象 key 与写入对象，不签发 URL、不写数据库。
3. key 使用报告前缀 + 合同 ID + 时间戳 + 随机后缀，避免覆盖历史报告。

对应：`文件集合/codex/4.C19MinIO报告上传.md`。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from minio import Minio

_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_PDF_CONTENT_TYPE = "application/pdf"
#: 报告允许的对象扩展名 → MIME（唯一处：key 后缀与 content-type 必须一致）
_REPORT_CONTENT_TYPES = {"docx": _DOCX_CONTENT_TYPE, "pdf": _PDF_CONTENT_TYPE}


def _secret_value(name: str, default: str) -> str:
    """读取连接凭据；`*_FILE` 优先于显式 env，空文件/不可读直接失败。"""
    file_path = os.getenv(f"{name}_FILE", "").strip()
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"{name}_FILE 不可读") from exc
        if not value:
            raise RuntimeError(f"{name}_FILE 为空")
        return value
    return os.getenv(name, default)


def get_minio_client() -> Minio:
    """按环境变量构建 MinIO 客户端（与 api-gateway 同源配置）。"""
    return Minio(
        endpoint=os.getenv("CG_MINIO_ENDPOINT", "localhost:9000"),
        access_key=_secret_value("CG_MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=_secret_value("CG_MINIO_SECRET_KEY", "minioadmin"),
        secure=os.getenv("CG_MINIO_SECURE", "false").lower() == "true",
    )


def upload_report(
    contract_id: str,
    data: bytes,
    content_type: str | None = None,
    *,
    extension: str = "docx",
) -> str:
    """上传渲染报告并返回 object key（C72：同一入口支持 docx/pdf）。

    Args:
        contract_id: 合同 ID；用于构造 `reports/{contract_id}/...` key。
        data: 报告字节内容。
        content_type: 对象 MIME；缺省按 `extension` 推导。
        extension: `docx`（默认，Word）或 `pdf`（PDF 导出）。

    Returns:
        MinIO object key，不包含 bucket 或预签名 URL。

    Raises:
        ValueError: 合同 ID 非法、报告内容为空或扩展名不支持。
    """
    _validate_contract_id(contract_id)
    if not data:
        raise ValueError("报告内容不能为空")
    suffix = extension.lower().lstrip(".")
    mime = _REPORT_CONTENT_TYPES.get(suffix)
    if mime is None:
        raise ValueError(f"不支持的报告扩展名：{extension}")

    bucket = os.getenv("CG_MINIO_BUCKET", "contracts")
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    key = f"reports/{contract_id}/{timestamp}_{uuid4().hex[:8]}.{suffix}"
    get_minio_client().put_object(
        bucket,
        key,
        BytesIO(data),
        length=len(data),
        content_type=content_type or mime,
    )
    return key


def _validate_contract_id(contract_id: str) -> None:
    """拒绝空 ID 与路径穿越片段。"""
    if not contract_id or any(char in contract_id for char in ("/", "\\")):
        raise ValueError("contract_id 非法")
    if ".." in contract_id:
        raise ValueError("contract_id 非法")


__all__ = ["get_minio_client", "upload_report"]
