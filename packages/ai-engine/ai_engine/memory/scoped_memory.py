"""分层 Memory —— 条款级局部上下文（仅保留当前条款近 3-5 轮）。

对应 data_scheme.md §10 ScopedClauseMemory：限制上下文窗口，避免多轮改写
引发上下文超载与状态丢失。
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

from .store import (
    InMemoryMemoryStore,
    MemoryStorePort,
    decode_model,
    encode_model,
)

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

    def __init__(
        self,
        max_turns: int = _MAX_TURNS,
        *,
        store: MemoryStorePort | None = None,
        contract_id: str = "default",
        key_prefix: str = "cg:mem",
        ttl_s: int | None = None,
    ) -> None:
        self._max_turns = max(1, max_turns)
        self._store = store or InMemoryMemoryStore()
        self._namespace = f"{key_prefix}:scoped:{contract_id}"
        self._ttl_s = ttl_s

    def _load(self, clause_id: str) -> ScopedClauseMemory:
        return decode_model(
            self._store.get(self._namespace, clause_id),
            ScopedClauseMemory,
        ) or ScopedClauseMemory(targetClauseId=clause_id)

    def _save(self, memory: ScopedClauseMemory) -> None:
        self._store.set(
            self._namespace,
            memory.targetClauseId,
            encode_model(memory),
            self._ttl_s,
        )

    def append(self, clause_id: str, turn: DialogTurn) -> None:
        memory = self._load(clause_id)
        buf = deque(memory.recentDialog, maxlen=self._max_turns)
        buf.append(turn)
        memory.recentDialog = list(buf)
        self._save(memory)

    def record_turn(
        self,
        clause_id: str,
        role: str,
        content: str,
        *,
        created_at: str,
    ) -> None:
        """追加一轮对话并自动执行窗口淘汰。"""
        self.append(
            clause_id,
            DialogTurn(role=role, content=content, createdAt=created_at),
        )

    def set_draft(self, clause_id: str, draft_after: str) -> None:
        """保存当前条款草稿。"""
        memory = self._load(clause_id)
        memory.draftAfter = draft_after
        self._save(memory)

    def snapshot(self, clause_id: str) -> ScopedClauseMemory:
        """导出条款级记忆副本。"""
        return self._load(clause_id)

    def get(self, clause_id: str) -> ScopedClauseMemory:
        """兼容旧入口：返回当前快照。"""
        return self.snapshot(clause_id)
