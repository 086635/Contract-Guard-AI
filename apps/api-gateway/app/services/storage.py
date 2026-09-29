"""MinIO 对象存储预签名适配器。

设计要点：
1. 内部 client 只做服务端对象操作，public client 只生成浏览器预签名 URL。
2. bucket 与连接参数统一来自 `app.config.settings`。
3. 测试可注入 internal/public client，避免依赖真实对象存储。

对应：`文件集合/codex/4.C8外部依赖适配器.md`。
"""

from __future__ import annotations

from datetime import timedelta

from minio import Minio

from app.config import settings
from app.services.ports import StoredObjectInfo


class MinioObjectStorage:
    """基于 MinIO SDK 的对象存储端口实现。"""

    def __init__(
        self,
        client: Minio | None = None,
        *,
        public_client: Minio | None = None,
        bucket: str | None = None,
    ) -> None:
        self._internal_client = client or self._build_client(
            settings.minio_endpoint
        )
        self._public_client = public_client or self._build_client(
            settings.resolved_minio_public_endpoint
        )
        self._bucket = bucket or settings.minio_bucket

    @staticmethod
    def _build_client(endpoint: str) -> Minio:
        """按 endpoint 构建独立签名的 MinIO client。"""
        return Minio(
            endpoint=endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            region=settings.minio_region,
        )

    @property
    def bucket(self) -> str:
        """返回当前桶名。"""
        return self._bucket

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        """生成 PUT 预签名 URL。"""
        self._validate_expiry(expires_s)
        return self._public_client.presigned_put_object(
            self._bucket,
            file_key,
            expires=timedelta(seconds=expires_s),
        )

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        """生成 GET 预签名 URL。"""
        self._validate_expiry(expires_s)
        return self._public_client.presigned_get_object(
            self._bucket,
            file_key,
            expires=timedelta(seconds=expires_s),
        )

    def stat_object(self, file_key: str) -> StoredObjectInfo:
        """读取对象大小与 ETag。"""
        stat = self._internal_client.stat_object(self._bucket, file_key)
        etag = str(getattr(stat, "etag", "")).strip('"')
        if not etag:
            raise ValueError(f"对象缺少 ETag: {file_key}")
        return StoredObjectInfo(
            size=int(getattr(stat, "size", 0)),
            etag=etag,
        )

    @staticmethod
    def _validate_expiry(expires_s: int) -> None:
        if expires_s <= 0:
            raise ValueError("expires_s 必须大于 0")


__all__ = ["MinioObjectStorage"]
