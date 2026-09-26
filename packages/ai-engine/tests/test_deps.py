"""ai-engine 依赖收口自证（T-B0 / C1）。

设计要点：
1. 同时验证真实导入、已安装版本与 pyproject 声明区间。
2. 用 AST 检查实际 import，避免注释或字符串造成误报。
3. ChatOpenAI 只构造、不发请求，保证用例可完全离线运行。

对应：`文件集合/codex/2.C1依赖收口.md` §⑦ / §⑧。
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

AI_ENGINE_ROOT = Path(__file__).resolve().parents[1] / "ai_engine"
PYPROJECT_PATH = Path(__file__).resolve().parents[1] / "pyproject.toml"

VERSION_BOUNDS = {
    "langgraph": ((1, 2), (2, 0)),
    "langchain-core": ((1, 6), (2, 0)),
    "langchain-openai": ((1, 1), (2, 0)),
    "openai": ((3, 3), (4, 0)),
}
REQUIRED_IMPORTS = (
    "ai_engine",
    "rag_core",
    "rag_contracts",
    "cg_observability",
    "langgraph",
    "langchain_core",
    "langchain_openai",
    "openai",
)
HEAVY_SDK_ROOTS = {
    "torch",
    "sentence_transformers",
    "jieba",
    "rank_bm25",
}
FORBIDDEN_DEPENDENCY_ROOTS = {
    "app",
    "apps",
    "worker",
}


def _version_pair(distribution: str) -> tuple[int, int]:
    """返回已安装发行版的主次版本号，供简单区间断言使用。"""
    version = importlib.metadata.version(distribution)
    match = re.match(r"^(\d+)\.(\d+)", version)
    if match is None:
        raise AssertionError(f"{distribution} 版本无法解析：{version!r}")
    return int(match.group(1)), int(match.group(2))


def _dependency_name(requirement: str) -> str:
    """从 PEP 508 依赖串中取出规范化前的发行版名。"""
    match = re.match(r"^[A-Za-z0-9_.-]+", requirement.strip())
    if match is None:
        raise AssertionError(f"依赖声明无法解析：{requirement!r}")
    return match.group(0).lower().replace("_", "-")


def _declared_dependencies() -> dict[str, str]:
    data = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    dependencies = data["project"]["dependencies"]
    return {_dependency_name(item): item for item in dependencies}


def _imported_roots(path: Path) -> set[str]:
    """解析单个 Python 文件的顶层 import 模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", maxsplit=1)[0] for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module is not None
        ):
            roots.add(node.module.split(".", maxsplit=1)[0])
    return roots


def _package_import_roots() -> dict[str, set[str]]:
    files = sorted(AI_ENGINE_ROOT.rglob("*.py"))
    assert files, f"未找到 ai_engine 源码：{AI_ENGINE_ROOT}"
    return {
        str(path.relative_to(AI_ENGINE_ROOT)): _imported_roots(path) for path in files
    }


def test_imports_and_versions() -> None:
    """四个 LLM 依赖和 workspace 包必须可导入且版本落在声明区间。"""
    for module_name in REQUIRED_IMPORTS:
        assert importlib.import_module(module_name) is not None

    for distribution, (lower, upper) in VERSION_BOUNDS.items():
        actual = _version_pair(distribution)
        assert lower <= actual < upper, (
            f"{distribution} 已安装版本 {actual} 不在 [{lower}, {upper}) 内"
        )


def test_workspace_imports() -> None:
    """workspace 兄弟包必须通过真实 import 链路，而非只在锁文件中存在。"""
    for module_name in ("ai_engine", "rag_core", "rag_contracts", "cg_observability"):
        assert importlib.import_module(module_name) is not None


def test_chat_openai_constructible() -> None:
    """ChatOpenAI 可构造；api_key 是占位符，不触发网络请求。"""
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        api_key="x",
    )
    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "deepseek-flash"


def test_no_open_ended_ranges() -> None:
    """关键 LLM 依赖必须同时声明下界和上界。"""
    dependencies = _declared_dependencies()
    for distribution in VERSION_BOUNDS:
        requirement = dependencies[distribution]
        assert ">=" in requirement and "<" in requirement, requirement


def test_no_heavy_sdk_imported_by_ai_engine() -> None:
    """ai-engine 不得直接引入本地重依赖。"""
    violations = {
        path: sorted(roots & HEAVY_SDK_ROOTS)
        for path, roots in _package_import_roots().items()
        if roots & HEAVY_SDK_ROOTS
    }
    assert not violations, violations


def test_ai_engine_does_not_import_worker() -> None:
    """ai-engine 不得反向依赖 worker / apps。"""
    violations = {
        path: sorted(roots & FORBIDDEN_DEPENDENCY_ROOTS)
        for path, roots in _package_import_roots().items()
        if roots & FORBIDDEN_DEPENDENCY_ROOTS
    }
    assert not violations, violations
