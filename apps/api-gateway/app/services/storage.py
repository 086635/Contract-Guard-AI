"""MinIO 对象存储预签名适配器。

设计要点：
1. 内部 client 只做服务端对象操作，public client 只生成浏览器预签名 URL。
2. bucket 与连接参数统一来自 `app.config.settings`。
3. 测试可注入 internal/public client，避免依赖真实对象存储。

对应：`文件集合/codex/4.C8外部依赖适配器.md`。
"""

from __future__ import annotations

import re
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


#: 合同对象键前缀（bucket 内唯一允许的上传子树；C56）。
UPLOAD_KEY_PREFIX = "contracts"

#: 服务端生成的合同对象键形态，与 `contract_service.generate_file_key` 同形：
#: `contracts/{yyyy}/{mm}/{uuid4().hex}-{safe_name}`。**唯一写入方**仍是契约服务，
#: 这里只负责「拒绝任意 key」的复核，不负责生成。
_UPLOAD_KEY_RE = re.compile(r"^contracts/\d{4}/\d{2}/[0-9a-f]{32}-[^/\\]+$")


class UploadObjectError(ValueError):
    """上传准入 / 对象复核失败（C56）。

    `status_code` 由路由映射为 HTTP 响应；`detail` 是**安全摘要**——
    只说明拒绝原因，不回显对象内容，也不回显用户提交的文件内容。
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def validate_upload_object(
    file_key: str,
    info: StoredObjectInfo | None = None,
    *,
    expected_size: int | None = None,
    expected_etag: str | None = None,
    max_bytes: int | None = None,
) -> None:
    """复核对象键与 `stat_object` 结果（C56 §⑤ `validate_upload_object`）。

    Args:
        file_key: 客户端回传的对象键；必须匹配服务端生成的固定前缀与形态。
        info: `stat_object` 的结果；为 `None` 时只复核 key 形态（stat 之前调用）。
        expected_size: 客户端声明的大小；`None` 表示未声明（不比对）。
        expected_etag: 客户端声明的 ETag；空串/`None` 表示未声明（不比对）。
        max_bytes: 服务端大小上限；`None` 表示本层不设上限。

    Raises:
        UploadObjectError: key 非法 / 超过上限 / 大小或 ETag 与 stat 不一致。
    """
    if not isinstance(file_key, str) or _UPLOAD_KEY_RE.match(file_key) is None:
        raise UploadObjectError(400, "fileKey 非法：必须由服务端 presign 生成")
    if info is None:
        return
    if max_bytes is not None and info.size > max_bytes:
        raise UploadObjectError(413, "文件超过大小上限")
    if expected_size is not None and int(expected_size) != info.size:
        raise UploadObjectError(409, "对象大小与声明不一致")
    if expected_etag and _normalize_etag(expected_etag) != _normalize_etag(info.etag):
        raise UploadObjectError(409, "对象 ETag 与声明不一致")


def _normalize_etag(value: str) -> str:
    """归一化 ETag：去空白、去引号、统一小写，避免大小写/引号导致的假不一致。"""
    return value.strip().strip('"').lower()


__all__ = [
    "UPLOAD_KEY_PREFIX",
    "MinioObjectStorage",
    "UploadObjectError",
    "validate_upload_object",
]
