"""C75 E2E 账号种子脚本（幂等；只创建/删除两个专用测试账号）。

用法::

    python scripts/seed_e2e_users.py              # 创建或重置 e2e-admin / e2e-viewer
    python scripts/seed_e2e_users.py --cleanup    # 只删除这两个账号（及其会话）
    python scripts/seed_e2e_users.py --dsn <dsn>  # 覆盖 CG_POSTGRES_DSN

安全约定：
1. 只操作两个固定测试账号（`CG_E2E_ADMIN_USERNAME` / `CG_E2E_VIEWER_USERNAME`，默认 `e2e-admin` / `e2e-viewer`）；
2. 密码来自 `CG_E2E_ADMIN_PASSWORD` / `CG_E2E_VIEWER_PASSWORD`（缺省为本地/CI 专用弱口令）；
3. 不打印任何密码或哈希；`--cleanup` 只删除这两个账号与它们的会话，不触碰真实用户。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _pkg in (
    "apps/api-gateway",
    "packages/rag-contracts",
    "packages/rag-core",
    "packages/cg-observability",
):
    _path = str(_REPO_ROOT / _pkg)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db.models import AuthSession, User  # noqa: E402
from app.db.repository.auth import UserCreate, UserRepository  # noqa: E402
from app.generated.schemas import Role  # noqa: E402
from app.services.security import hash_password  # noqa: E402

DEFAULT_ADMIN = "e2e-admin"
DEFAULT_VIEWER = "e2e-viewer"
DEFAULT_ADMIN_PASSWORD = "e2e-admin-pass"
DEFAULT_VIEWER_PASSWORD = "e2e-viewer-pass"


def _accounts() -> list[tuple[str, str, Role, str]]:
    return [
        (
            os.getenv("CG_E2E_ADMIN_USERNAME", DEFAULT_ADMIN),
            os.getenv("CG_E2E_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
            Role.admin,
            "E2E 管理员",
        ),
        (
            os.getenv("CG_E2E_VIEWER_USERNAME", DEFAULT_VIEWER),
            os.getenv("CG_E2E_VIEWER_PASSWORD", DEFAULT_VIEWER_PASSWORD),
            Role.viewer,
            "E2E 只读用户",
        ),
    ]


async def _cleanup(session_factory) -> None:
    usernames = [name for name, *_ in _accounts()]
    async with session_factory() as session:
        user_ids = (
            await session.scalars(select(User.id).where(User.username.in_(usernames)))
        ).all()
        if user_ids:
            await session.execute(
                delete(AuthSession).where(AuthSession.user_id.in_(user_ids))
            )
            await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()
    print(f"[seed] cleanup 完成：删除 {len(user_ids)} 个测试账号及其会话")


async def _seed(session_factory) -> None:
    async with session_factory() as session:
        repo = UserRepository(session)
        for username, password, role, display_name in _accounts():
            existing = await repo.get_by_username(username)
            if existing is not None:
                # 重置口令/角色，保证 E2E 可重复执行（只动测试账号）
                record = await session.get(User, existing.id)
                assert record is not None
                record.password_hash = hash_password(password)
                record.role = role
                record.is_active = True
                record.display_name = display_name
                record.failed_login_attempts = 0
                record.locked_until = None
                print(f"[seed] 重置 {username}（role={role.value}）")
                continue
            await repo.create(
                UserCreate(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                    is_active=True,
                    display_name=display_name,
                )
            )
            print(f"[seed] 创建 {username}（role={role.value}）")
        await session.commit()


def _inject_password(dsn: str, password: str | None) -> str:
    """与 `app.config` 同款语义：`CG_POSTGRES_PASSWORD_FILE` 覆盖 DSN 口令位。"""
    if not password or "://" not in dsn or "@" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    credentials, host = rest.split("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:{password}@{host}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C75 E2E 测试账号种子/清理")
    parser.add_argument("--dsn", default=os.getenv("CG_POSTGRES_DSN", ""))
    parser.add_argument("--cleanup", action="store_true", help="只删除测试账号")
    args = parser.parse_args(argv)

    password_file = os.getenv("CG_POSTGRES_PASSWORD_FILE", "")
    if password_file and Path(password_file).is_file():
        args.dsn = _inject_password(
            args.dsn, Path(password_file).read_text(encoding="utf-8").strip()
        )

    if not args.dsn:
        print("[seed] 缺少 --dsn 或 CG_POSTGRES_DSN", file=sys.stderr)
        return 2

    async def _run() -> None:
        engine = create_async_engine(args.dsn, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            if args.cleanup:
                await _cleanup(session_factory)
            else:
                await _seed(session_factory)
        finally:
            await engine.dispose()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
