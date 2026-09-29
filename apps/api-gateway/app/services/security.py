"""C45 认证安全原语。

本模块是密码哈希、access JWT 和 refresh token 哈希的唯一定义处：
- 密码只通过 Argon2id 哈希，不保存明文；
- JWT 的签名密钥、issuer、audience 和 TTL 均来自配置；
- refresh token 使用高熵随机值，数据库只保存 SHA-256 哈希。
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel, ValidationError

from app.config import Settings, settings
from app.db.repository.auth import UserRecord
from app.generated.schemas import Role


class AuthSecurityError(ValueError):
    """认证安全原语的领域异常基类。"""


class PasswordHashError(AuthSecurityError):
    """密码哈希或校验失败。"""


class TokenValidationError(AuthSecurityError):
    """JWT 校验失败；对外不泄露具体失败原因。"""


class RefreshTokenError(AuthSecurityError):
    """refresh token 生成或哈希失败。"""


class TokenClaims(BaseModel):
    """已通过签名和完整 claims 校验的 access token。"""

    sub: UUID
    role: Role
    typ: Literal["access"]
    iss: str
    aud: str
    iat: datetime
    exp: datetime


def _utc_now() -> datetime:
    """返回当前 UTC 时间，供生产路径和测试时钟共同使用。"""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """把时间转换为 UTC，拒绝 naive datetime。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


class PasswordService:
    """Argon2id 密码服务；允许测试注入自定义 hasher。"""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher(
            time_cost=3,
            memory_cost=65536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
        )

    def hash(self, raw_password: str) -> str:
        """生成 Argon2id 哈希。"""
        if not raw_password:
            raise PasswordHashError("password 不能为空")
        try:
            return self._hasher.hash(raw_password)
        except Exception as exc:
            raise PasswordHashError("密码哈希失败") from exc

    def verify(self, raw_password: str, password_hash: str) -> bool:
        """校验密码；哈希格式损坏或密码错误均返回 False。"""
        if not raw_password or not password_hash:
            return False
        try:
            return self._hasher.verify(password_hash, raw_password)
        except (VerifyMismatchError, InvalidHashError, VerificationError):
            return False


_DEFAULT_PASSWORD_SERVICE = PasswordService()


def hash_password(raw_password: str) -> str:
    """使用默认 Argon2id 参数生成密码哈希。"""
    return _DEFAULT_PASSWORD_SERVICE.hash(raw_password)


def verify_password(raw_password: str, password_hash: str) -> bool:
    """使用默认 Argon2id 参数校验密码。"""
    return _DEFAULT_PASSWORD_SERVICE.verify(raw_password, password_hash)


def generate_refresh_token(num_bytes: int = 48) -> str:
    """生成高熵 refresh token 明文；调用方只允许把哈希落库。"""
    if num_bytes < 32:
        raise RefreshTokenError("refresh token 至少需要 256 bit 熵")
    return secrets.token_urlsafe(num_bytes)


def hash_refresh_token(raw_token: str) -> str:
    """计算 refresh token 的 SHA-256 哈希，用于唯一检索和撤销。"""
    if not raw_token:
        raise RefreshTokenError("refresh token 不能为空")
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class JwtService:
    """access JWT 签发与校验服务。"""

    def __init__(
        self,
        *,
        secret: str,
        algorithm: str = "HS256",
        issuer: str = "contract-guard",
        audience: str = "contract-guard-api",
        access_ttl_seconds: int = 900,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not secret or not secret.strip():
            raise AuthSecurityError("jwt secret 不能为空")
        if access_ttl_seconds <= 0:
            raise AuthSecurityError("jwt access ttl 必须大于 0")
        self._secret = secret
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self._access_ttl_seconds = access_ttl_seconds
        self._now = now or _utc_now

    @property
    def access_ttl_seconds(self) -> int:
        """返回 access token TTL，供 TokenData.expiresIn 使用。"""
        return self._access_ttl_seconds

    @classmethod
    def from_settings(cls, app_settings: Settings | None = None) -> JwtService:
        """从统一配置构造 JWT 服务。"""
        current = app_settings or settings
        return cls(
            secret=current.jwt_secret,
            algorithm=current.jwt_algorithm,
            issuer=current.jwt_issuer,
            audience=current.jwt_audience,
            access_ttl_seconds=current.jwt_access_ttl_seconds,
        )

    def issue_access(self, user: UserRecord) -> str:
        """按当前时钟签发短时 access token。"""
        now = _as_utc(self._now())
        expires_at = now + timedelta(seconds=self._access_ttl_seconds)
        payload = {
            "sub": str(user.id),
            "role": user.role.value,
            "typ": "access",
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        try:
            return jwt.encode(payload, self._secret, algorithm=self._algorithm)
        except Exception as exc:
            raise TokenValidationError("access token 签发失败") from exc

    def verify_access(self, token: str) -> TokenClaims:
        """校验签名、issuer、audience、exp、iat 和 typ。"""
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                options={
                    "require": ["sub", "role", "typ", "iss", "aud", "iat", "exp"],
                    # 时间 claims 使用注入时钟手动校验，保证测试可复现。
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenValidationError("access token 已过期") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenValidationError("access token 无效") from exc

        try:
            issued_at = datetime.fromtimestamp(payload["iat"], UTC)
            expires_at = datetime.fromtimestamp(payload["exp"], UTC)
            current_time = _as_utc(self._now())
            if issued_at > current_time:
                raise TokenValidationError("access token 尚未生效")
            if expires_at <= current_time:
                raise TokenValidationError("access token 已过期")
            return TokenClaims(
                sub=UUID(payload["sub"]),
                role=Role(payload["role"]),
                typ=payload["typ"],
                iss=payload["iss"],
                aud=payload["aud"],
                iat=issued_at,
                exp=expires_at,
            )
        except TokenValidationError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise TokenValidationError("access token claims 无效") from exc


__all__ = [
    "AuthSecurityError",
    "JwtService",
    "PasswordHashError",
    "PasswordService",
    "RefreshTokenError",
    "TokenClaims",
    "TokenValidationError",
    "generate_refresh_token",
    "hash_password",
    "hash_refresh_token",
    "verify_password",
]