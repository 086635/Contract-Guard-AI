"""MinIO 对象存储客户端（合同源文件 / 报告导出）。"""

from __future__ import annotations

import os

from minio import Minio


def get_minio_client() -> Minio:
    """按环境变量构建 MinIO 客户端（与 api-gateway 同源配置）。"""
    return Minio(
        endpoint=os.getenv("CG_MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.getenv("CG_MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("CG_MINIO_SECRET_KEY", "minioadmin"),
        secure=os.getenv("CG_MINIO_SECURE", "false").lower() == "true",
    )


def upload_report(contract_id: str, data: bytes, content_type: str = "application/pdf") -> str:
    """上传渲染报告并返回 file_key。TODO：实现上传逻辑。"""
    raise NotImplementedError("upload_report 待实现")
