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
from uuid import uuid4

from minio import Minio

_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def get_minio_client() -> Minio:
    """按环境变量构建 MinIO 客户端（与 api-gateway 同源配置）。"""
    return Minio(
        endpoint=os.getenv("CG_MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.getenv("CG_MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("CG_MINIO_SECRET_KEY", "minioadmin"),
        secure=os.getenv("CG_MINIO_SECURE", "false").lower() == "true",
    )


def upload_report(
    contract_id: str,
    data: bytes,
    content_type: str = _DOCX_CONTENT_TYPE,
) -> str:
    """上传渲染报告并返回 object key。

    Args:
        contract_id: 合同 ID；用于构造 `reports/{contract_id}/...` key。
        data: DOCX 字节内容。
        content_type: 对象 MIME，默认 DOCX。

    Returns:
        MinIO object key，不包含 bucket 或预签名 URL。

    Raises:
        ValueError: 合同 ID 非法或报告内容为空。
    """
    _validate_contract_id(contract_id)
    if not data:
        raise ValueError("报告内容不能为空")

    bucket = os.getenv("CG_MINIO_BUCKET", "contracts")
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    key = f"reports/{contract_id}/{timestamp}_{uuid4().hex[:8]}.docx"
    get_minio_client().put_object(
        bucket,
        key,
        BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return key


def _validate_contract_id(contract_id: str) -> None:
    """拒绝空 ID 与路径穿越片段。"""
    if not contract_id or any(char in contract_id for char in ("/", "\\")):
        raise ValueError("contract_id 非法")
    if ".." in contract_id:
        raise ValueError("contract_id 非法")


__all__ = ["get_minio_client", "upload_report"]
