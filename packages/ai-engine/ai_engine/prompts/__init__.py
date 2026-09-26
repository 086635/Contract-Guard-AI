"""Prompt 模版库与加载器。

所有 Prompt 模版统一存放、按需加载，支持 {{placeholder}} 占位替换；角色 /
温度 / 风险容忍度由 SystemAuditConfiguration.promptConfig（§8.3）驱动。
"""

from __future__ import annotations

from pathlib import Path

from .loader import PromptLibrary, load_prompt

PROMPT_NAMES: tuple[str, ...] = (
    "classify",
    "risk",
    "conflict",
    "rewrite",
)
OPTIONAL_PROMPT_NAMES: tuple[str, ...] = ("extract",)


def templates_dir() -> str:
    """返回包内模板目录的绝对路径。"""
    return str(Path(__file__).resolve().parent / "templates")


def load_default_library() -> PromptLibrary:
    """加载包内全部模板；目录缺失时返回空库。"""
    return PromptLibrary(templates_dir())


__all__ = [
    "OPTIONAL_PROMPT_NAMES",
    "PROMPT_NAMES",
    "PromptLibrary",
    "load_default_library",
    "load_prompt",
    "templates_dir",
]
