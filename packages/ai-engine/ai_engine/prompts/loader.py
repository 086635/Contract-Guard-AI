"""Prompt 模版加载器。

设计要点：
- 模版以 Jinja2 / 简单 {{key}} 占位符编写，集中管理。
- 运行时由 PromptRoleConfig（§8.3）注入 roleName / systemPrompt / temperature。
- 禁止在业务代码里散落硬编码 Prompt 字符串。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class PromptTemplate:
    name: str
    template: str

    def render(self, **kwargs: str) -> str:
        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            if key not in kwargs:
                raise KeyError(f"Prompt 模版缺少占位符: {key}")
            return str(kwargs[key])

        return _PLACEHOLDER.sub(repl, self.template)


class PromptLibrary:
    """从目录加载所有 .prompt 模版。"""

    def __init__(self, templates_dir: str | None = None) -> None:
        self._templates: dict[str, PromptTemplate] = {}
        if templates_dir:
            self.load_dir(templates_dir)

    def load_dir(self, templates_dir: str) -> None:
        base = Path(templates_dir)
        if not base.exists():
            return
        for path in base.glob("*.prompt"):
            self._templates[path.stem] = PromptTemplate(
                name=path.stem, template=path.read_text(encoding="utf-8")
            )

    def get(self, name: str) -> PromptTemplate:
        if name not in self._templates:
            raise KeyError(f"未找到 Prompt 模版: {name}")
        return self._templates[name]


def load_prompt(name: str, templates_dir: str | None = None, **kwargs: str) -> str:
    """便捷函数：加载并渲染指定模版。"""
    lib = PromptLibrary(templates_dir)
    return lib.get(name).render(**kwargs)
