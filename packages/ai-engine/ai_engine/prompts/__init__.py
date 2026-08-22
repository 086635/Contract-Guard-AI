"""Prompt 模版库与加载器。

所有 Prompt 模版统一存放、按需加载，支持 {{placeholder}} 占位替换；角色 /
温度 / 风险容忍度由 SystemAuditConfiguration.promptConfig（§8.3）驱动。
"""

from .loader import PromptLibrary, load_prompt

__all__ = ["PromptLibrary", "load_prompt"]
