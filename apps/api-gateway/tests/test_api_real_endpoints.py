"""C17 OpenAPI operation 与生产路由矩阵守卫。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.deps import get_service_context
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENAPI_PATH = REPO_ROOT / "packages" / "schema" / "openapi.yaml"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


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


def _iter_routes(router) -> Iterator[APIRoute]:
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _iter_routes(included)


def _dependency_calls(dependant: Dependant) -> set[object]:
    calls: set[object] = set()
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            calls.add(dependency.call)
        calls.update(_dependency_calls(dependency))
    return calls


def _production_operations() -> set[tuple[str, str]]:
    return {
        (method.upper(), _normalize_path(path))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }


def test_openapi_operation_inventory_matches_production_app() -> None:
    expected = _openapi_operations()
    actual = _production_operations()

    assert len(expected) == 19
    assert actual == expected


def test_every_v1_route_uses_service_context() -> None:
    missing: list[str] = []
    for route in _iter_routes(app.router):
        if not route.endpoint.__module__.startswith("app.api.v1"):
            continue
        if get_service_context not in _dependency_calls(route.dependant):
            for method in sorted(route.methods):
                missing.append(f"{method} {route.path}")

    assert missing == []
