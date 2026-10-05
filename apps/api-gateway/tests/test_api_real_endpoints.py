"""C17/C38 OpenAPI operation 与生产路由矩阵守卫。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.deps import get_current_user, get_service_context
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = REPO_ROOT / "packages" / "schema" / "openapi.yaml"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
UPPER_HTTP_METHODS = {method.upper() for method in HTTP_METHODS}


def _normalize_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _openapi_operations() -> set[tuple[str, str]]:
    operations: set[tuple[str, str]] = set()
    current_path: str | None = None
    in_paths = False
    for raw_line in OPENAPI_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line == "paths:":
            in_paths = True
            continue
        if not in_paths:
            continue
        if line.startswith("  /") and line.endswith(":"):
            current_path = line.strip()[:-1]
            continue
        method = line.strip().removesuffix(":")
        if current_path is not None and method in HTTP_METHODS:
            operations.add(
                (
                    method.upper(),
                    _normalize_path(f"/api/v1{current_path}"),
                )
            )
    return operations


def _inherited_dependencies(node) -> tuple[object, ...]:
    """取 ``include_context`` / ``APIRouter`` 上声明的依赖调用（``Depends(...)``）。"""
    calls: list[object] = []
    for dependency in getattr(node, "dependencies", None) or ():
        call = getattr(dependency, "dependency", None)
        if call is not None:
            calls.append(call)
    return tuple(calls)


def _iter_routes(
    router,
    prefix: str = "",
    inherited: tuple[object, ...] = (),
) -> Iterator[tuple[APIRoute, str, tuple[object, ...]]]:
    """遍历 FastAPI 路由树，产出 ``(route, 有效路径前缀, 继承依赖)``。

    FastAPI 0.115+ 起 ``include_router`` 不再把子路由展平进父 router，而是插入
    ``_IncludedRouter`` 包装节点：父级 prefix 与 ``dependencies=`` 只挂在包装节点的
    ``include_context`` 上，且子 ``APIRoute.path`` 已含模块自身 prefix（如
    ``/contracts``）。旧实现只递归 ``original_router`` 丢掉前缀，导致 ``/api/v1``
    过滤恒空、鉴权守卫空转（C75-C① P1-2）。
    """

    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route, prefix, inherited
            continue
        included = getattr(route, "original_router", None)
        if included is None:
            continue
        context = getattr(route, "include_context", None)
        context_prefix = (getattr(context, "prefix", "") or "") or prefix
        dependencies = (
            *_inherited_dependencies(context),
            *_inherited_dependencies(included),
        )
        yield from _iter_routes(included, context_prefix, inherited + dependencies)


def _dependency_calls(dependant: Dependant) -> set[object]:
    calls: set[object] = set()
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            calls.add(dependency.call)
        calls.update(_dependency_calls(dependency))
    return calls


def _route_dependency_calls(route: APIRoute, inherited: tuple[object, ...]) -> set[object]:
    """路由自身依赖树 ∪ 祖先 ``include_router`` / ``APIRouter`` 声明的依赖。"""
    return set(inherited) | _dependency_calls(route.dependant)


def _v1_operations(router) -> set[tuple[str, str]]:
    """walker 视角下的 v1 操作集（用于与 OpenAPI 比对，防止守卫再次空转）。"""
    operations: set[tuple[str, str]] = set()
    for route, prefix, _inherited in _iter_routes(router):
        path = _normalize_path(prefix + route.path)
        if not path.startswith("/api/v1"):
            continue
        for method in route.methods:
            if method in UPPER_HTTP_METHODS:
                operations.add((method, path))
    return operations


def _missing_auth_operations(router) -> list[str]:
    """返回缺少 ``get_current_user`` 依赖的 v1 操作（登录/刷新/SSE 订阅豁免）。"""
    public = {
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/refresh"),
        # C64：SSE 用短期 streamToken 或 Bearer 双凭据（见 app/api/v1/tasks.py::stream_principal），
        # 因此不在「必须挂 get_current_user」的集合里，但**仍然需要凭据**、不放宽匿名订阅。
        ("GET", _normalize_path("/api/v1/tasks/{taskId}/stream")),
    }
    missing: list[str] = []
    for route, prefix, inherited in _iter_routes(router):
        path = _normalize_path(prefix + route.path)
        if not path.startswith("/api/v1"):
            continue
        present = _route_dependency_calls(route, inherited)
        for method in sorted(route.methods):
            if (method, path) in public:
                continue
            if get_current_user not in present:
                missing.append(f"{method} {path}")
    return missing


def _production_operations() -> set[tuple[str, str]]:
    return {
        (method.upper(), _normalize_path(path))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
        if method.upper() in UPPER_HTTP_METHODS
    }


def test_openapi_operation_inventory_matches_production_app() -> None:
    expected = _openapi_operations()
    actual = _production_operations()

    # C58：+2（preview / preview-retry）；C64：+1（POST /tasks/{taskId}/stream-token）→ 31
    # C70：+4（GET/POST /admin/standard-clauses、POST …/rebuild、PATCH …/{clauseId}）→ 35
    # T-D6 maintenance: POST parse/retry, POST settings/rules, POST feedback/export → 38
    # T-D6 三库维护：+2（官方来源状态 / 检索预览）→ 40。
    assert len(expected) == 40
    assert actual == expected


def test_route_walker_discovers_every_openapi_v1_operation() -> None:
    """非空转自证：walker 必须发现与 OpenAPI 完全一致的 v1 操作集。

    若 FastAPI 路由树结构再变（正是 P1-2 空转的成因），这里会先红灯，而不是让鉴权守卫
    静默变成「0 条路由 → missing == []」。
    """
    discovered = _v1_operations(app.router)
    expected = _production_operations()

    assert discovered, "route walker 未发现任何 v1 路由（守卫空转）"
    assert discovered == expected, (
        f"route walker 与 OpenAPI 漂移：missing={sorted(expected - discovered)} "
        f"extra={sorted(discovered - expected)}"
    )


def test_all_v1_routes_except_login_refresh_require_auth() -> None:
    """除登录/刷新/SSE 订阅外，所有 v1 路由必须经过 Bearer 当前用户依赖。"""
    assert _missing_auth_operations(app.router) == []


def test_auth_guard_detects_unauthenticated_v1_route() -> None:
    """负例自证（C75-C① P1-2）：缺 get_current_user 的 v1 路由必须被守卫列出。

    同源正例证明守卫不是「恒报错」：挂上依赖后必须放行。
    """
    unguarded = FastAPI()
    unguarded_router = APIRouter(prefix="/api/v1")

    @unguarded_router.get("/probe-unguarded")
    async def _probe_unguarded() -> dict[str, str]:  # pragma: no cover - 仅守卫探针
        return {"ok": "1"}

    unguarded.include_router(unguarded_router)

    guarded = FastAPI()
    guarded_router = APIRouter(prefix="/api/v1", dependencies=[Depends(get_current_user)])

    @guarded_router.get("/probe-guarded")
    async def _probe_guarded() -> dict[str, str]:  # pragma: no cover - 仅守卫探针
        return {"ok": "1"}

    guarded.include_router(guarded_router)

    assert _missing_auth_operations(unguarded.router) == ["GET /api/v1/probe-unguarded"]
    assert _missing_auth_operations(guarded.router) == []


def test_every_v1_route_uses_service_context() -> None:
    missing: list[str] = []
    for route, prefix, inherited in _iter_routes(app.router):
        if not route.endpoint.__module__.startswith("app.api.v1"):
            continue
        if route.endpoint.__module__ in {"app.api.v1.auth", "app.api.v1.audit"}:
            continue
        if get_service_context not in _route_dependency_calls(route, inherited):
            path = _normalize_path(prefix + route.path)
            for method in sorted(route.methods):
                missing.append(f"{method} {path}")

    assert missing == []
