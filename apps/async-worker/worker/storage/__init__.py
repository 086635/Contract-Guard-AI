"""Worker 侧存储客户端（MinIO / Qdrant 交互）。"""

from .av_scan import ClamdScanner, ScanResult, av_mode, detect_document_type
from .minio_client import get_minio_client, upload_report
from .qdrant_client import get_qdrant_client

__all__ = [
    "ClamdScanner",
    "ScanResult",
    "av_mode",
    "detect_document_type",
    "get_minio_client",
    "get_qdrant_client",
    "upload_report",
]
