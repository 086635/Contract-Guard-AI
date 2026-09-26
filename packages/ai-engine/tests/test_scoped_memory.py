"""分层记忆单测（T-B5 / C12）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ai_engine.agents.rewriter import ScopedClauseMemory as RewriterMemory
from ai_engine.memory import (
    GlobalContractState,
    GlobalStateManager,
    Money,
    ScopedClauseMemory,
    ScopedMemoryManager,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_scoped_memory_single_definition() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "ai_engine" / "agents" / "rewriter.py"
    ).read_text(encoding="utf-8")
    assert "class ScopedClauseMemory" not in source
    assert RewriterMemory is ScopedClauseMemory


def test_record_turn_window_capped_at_five() -> None:
    manager = ScopedMemoryManager()
    for index in range(6):
        manager.record_turn(
            "c1",
            "USER",
            f"turn-{index}",
            created_at=_now(),
        )
    snapshot = manager.snapshot("c1")
    assert len(snapshot.recentDialog) == 5
    assert [turn.content for turn in snapshot.recentDialog] == [
        "turn-1",
        "turn-2",
        "turn-3",
        "turn-4",
        "turn-5",
    ]


def test_snapshot_contains_recent_turns() -> None:
    manager = ScopedMemoryManager()
    created_at = _now()
    manager.record_turn("c1", "USER", "问题", created_at=created_at)
    snapshot = manager.snapshot("c1")
    assert snapshot.targetClauseId == "c1"
    assert snapshot.recentDialog[0].role == "USER"
    assert snapshot.recentDialog[0].content == "问题"
    assert snapshot.recentDialog[0].createdAt == created_at


def test_set_draft_roundtrip() -> None:
    manager = ScopedMemoryManager()
    manager.set_draft("c1", "新草稿")
    assert manager.snapshot("c1").draftAfter == "新草稿"


def test_clause_scopes_isolation() -> None:
    manager = ScopedMemoryManager()
    manager.record_turn("c1", "USER", "one", created_at=_now())
    manager.record_turn("c2", "USER", "two", created_at=_now())
    assert manager.snapshot("c1").recentDialog[0].content == "one"
    assert manager.snapshot("c2").recentDialog[0].content == "two"


def test_append_compatibility() -> None:
    from ai_engine.memory import DialogTurn

    manager = ScopedMemoryManager()
    manager.append(
        "c1",
        DialogTurn(role="ASSISTANT", content="兼容", createdAt=_now()),
    )
    assert manager.get("c1").recentDialog[0].content == "兼容"


def test_global_state_save_load() -> None:
    manager = GlobalStateManager()
    state = GlobalContractState(
        contractId="C1",
        meta={
            "partyA": "甲",
            "partyB": "乙",
            "amount": Money(
                amount="100.00",
                currency="CNY",
                amountMinorUnit=10000,
            ),
        },
        activeVersionId="v1",
    )
    manager.save(state)
    restored = manager.load("C1")
    assert restored is not None
    assert restored.model_dump() == state.model_dump()
    assert manager.load("missing") is None


def test_no_persistence_imports() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "ai_engine"
        / "memory"
        / "scoped_memory.py"
    ).read_text(encoding="utf-8")
    assert "redis" not in source
    assert "postgres" not in source
