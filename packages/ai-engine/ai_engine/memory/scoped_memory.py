"""分层 Memory —— 条款级局部上下文（仅保留当前条款近 3-5 轮）。

对应 data_scheme.md §10 ScopedClauseMemory：限制上下文窗口，避免多轮改写
引发上下文超载与状态丢失。
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

_MAX_TURNS = 5  # 仅保留最近 3-5 轮


class DialogTurn(BaseModel):
    role: str  # USER | ASSISTANT
    content: str
    createdAt: str  # ISO 8601 UTC


class ScopedClauseMemory(BaseModel):
    """§10 条款级记忆"""

    targetClauseId: str
    recentDialog: list[DialogTurn] = Field(default_factory=list)
    draftAfter: str | None = None


class ScopedMemoryManager:
    """条款级记忆管理：滑动窗口维护近 N 轮对话。"""

    def __init__(self, max_turns: int = _MAX_TURNS) -> None:
        self._max_turns = max_turns
        self._mem: dict[str, deque[DialogTurn]] = {}

    def append(self, clause_id: str, turn: DialogTurn) -> None:
        buf = self._mem.setdefault(clause_id, deque(maxlen=self._max_turns))
        buf.append(turn)

    def get(self, clause_id: str) -> ScopedClauseMemory:
        buf = self._mem.get(clause_id, deque())
        return ScopedClauseMemory(
            targetClauseId=clause_id,
            recentDialog=list(buf),
            draftAfter=None,
        )
