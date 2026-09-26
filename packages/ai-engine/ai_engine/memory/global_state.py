"""分层 Memory —— 全局合同状态（跨会话稳定）。

对应 data_scheme.md §10 GlobalContractState：保存合同元信息、全局 AST 拓扑
与当前激活版本，避免多轮对话反复重载全量上下文。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .store import (
    InMemoryMemoryStore,
    MemoryStorePort,
    decode_model,
    encode_model,
)


class Money(BaseModel):
    amount: str
    currency: str  # CNY | USD | EUR | OTHER
    amountMinorUnit: int


class ClauseNode(BaseModel):
    """全局 AST 拓扑节点"""

    id: str
    parentId: str | None = None
    childrenIds: list[str] = Field(default_factory=list)
    label: str
    text: str
    level: int = 0


class GlobalContractState(BaseModel):
    """§10 全局合同状态"""

    contractId: str
    meta: dict  # { partyA, partyB, amount: Money, contractType }
    astTopology: list[ClauseNode] = Field(default_factory=list)
    activeVersionId: str


class GlobalStateManager:
    """全局状态读写（可后端接 Redis / Postgres）。"""

    def __init__(
        self,
        store: MemoryStorePort | None = None,
        *,
        key_prefix: str = "cg:mem",
        ttl_s: int | None = None,
    ) -> None:
        self._store = store or InMemoryMemoryStore()
        self._namespace = f"{key_prefix}:global"
        self._ttl_s = ttl_s

    def save(self, state: GlobalContractState) -> None:
        self._store.set(
            self._namespace,
            state.contractId,
            encode_model(state),
            self._ttl_s,
        )

    def load(self, contract_id: str) -> GlobalContractState | None:
        return decode_model(
            self._store.get(self._namespace, contract_id),
            GlobalContractState,
        )
