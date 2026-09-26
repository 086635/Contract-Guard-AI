"""分层 Memory 管理器：全局状态 + 局部上下文。"""

from .checkpointer import build_checkpointer
from .factory import MemorySettings, build_memory_store
from .global_state import ClauseNode, GlobalContractState, GlobalStateManager, Money
from .scoped_memory import DialogTurn, ScopedClauseMemory, ScopedMemoryManager
from .store import InMemoryMemoryStore, MemoryStorePort

__all__ = [
    "ClauseNode",
    "DialogTurn",
    "GlobalContractState",
    "GlobalStateManager",
    "InMemoryMemoryStore",
    "MemorySettings",
    "MemoryStorePort",
    "Money",
    "ScopedClauseMemory",
    "ScopedMemoryManager",
    "build_checkpointer",
    "build_memory_store",
]
