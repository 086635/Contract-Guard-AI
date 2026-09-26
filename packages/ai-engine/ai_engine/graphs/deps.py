"""审核图依赖容器。

设计要点：
1. 依赖对象只通过闭包注入节点，不进入可序列化 state。
2. RAG Tool 允许缺省，便于图骨架先编译，业务节点再显式降级。
3. LLM 与 Prompt 库均为惰性消费，不在构图期构造。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.language_models.chat_models import BaseChatModel

from ai_engine.memory import ScopedClauseMemory
from ai_engine.prompts.loader import PromptLibrary
from rag_contracts.agent_tool import AgentRAGToolResult


@dataclass(frozen=True, slots=True)
class GraphDeps:
    """审核图依赖容器。"""

    rag_tool: Callable[..., AgentRAGToolResult] | None = None
    llm: BaseChatModel | None = None
    prompt_lib: PromptLibrary | None = None
    memory: ScopedClauseMemory | None = None
    max_clauses: int = 100
    max_conflict_pairs: int = 50
    conflict_batch_size: int = 10


__all__ = ["GraphDeps"]
