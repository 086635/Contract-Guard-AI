"""C17 生产源码去 Mock 守卫。"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _python_files() -> list[Path]:
    files = list((APP_ROOT / "api" / "v1").glob("*.py"))
    files.extend((APP_ROOT / "services").glob("*.py"))
    return [
        path for path in files if path.name not in {"mock_factory.py", "__init__.py"}
    ]


def test_production_routes_and_services_do_not_import_or_call_mock_factory() -> None:
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "app.services.mock_factory"
                or any(alias.name == "MockDataFactory" for alias in node.names)
            ):
                offenders.append(f"{path}: import")
            if isinstance(node, ast.Name) and node.id == "MockDataFactory":
                offenders.append(f"{path}:{node.lineno}: call/reference")

    assert offenders == []


def test_mock_factory_remains_only_in_historical_test_harness() -> None:
    production_matches = [
        path
        for path in _python_files()
        if "MockDataFactory" in path.read_text(encoding="utf-8")
    ]

    assert production_matches == []
