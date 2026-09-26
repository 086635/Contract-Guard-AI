"""记忆持久化单测（T-B6 / C13）。"""

from __future__ import annotations

from datetime import UTC, datetime

from langgraph.checkpoint.memory import InMemorySaver

from ai_engine.memory import (
    DialogTurn,
    GlobalContractState,
    GlobalStateManager,
    InMemoryMemoryStore,
    MemorySettings,
    ScopedClauseMemory,
    ScopedMemoryManager,
    build_checkpointer,
    build_memory_store,
)
from ai_engine.memory.store import decode_model, encode_model


def _now() -> str:
    return datetime.now(UTC).isoformat()


def test_inmemory_roundtrip() -> None:
    store = InMemoryMemoryStore()
    store.set("ns", "k", "v")
    assert store.get("ns", "k") == "v"
    store.delete("ns", "k")
    assert store.get("ns", "k") is None


def test_shared_store_across_managers() -> None:
    store = InMemoryMemoryStore()
    first = ScopedMemoryManager(store=store, contract_id="C1")
    second = ScopedMemoryManager(store=store, contract_id="C1")
    first.record_turn("c1", "USER", "你好", created_at=_now())
    assert second.snapshot("c1").recentDialog[0].content == "你好"


def test_serialization_roundtrip_with_draft() -> None:
    memory = ScopedClauseMemory(
        targetClauseId="c1",
        recentDialog=[DialogTurn(role="USER", content="改成 15 日", createdAt=_now())],
        draftAfter="草稿",
    )
    restored = decode_model(encode_model(memory), ScopedClauseMemory)
    assert restored == memory


def test_ttl_expiry_with_fake_clock() -> None:
    clock = [0.0]
    store = InMemoryMemoryStore(clock=lambda: clock[0])
    store.set("ns", "expires", "v", ttl_s=10)
    store.set("ns", "forever", "v", ttl_s=None)
    clock[0] = 11.0
    assert store.get("ns", "expires") is None
    assert store.get("ns", "forever") == "v"


def test_redis_backend_degrades() -> None:
    settings = MemorySettings(
        _env_file=None,
        backend="redis",
        redis_url="redis://127.0.0.1:1",
    )
    store = build_memory_store(settings)
    assert isinstance(store, InMemoryMemoryStore)
    store.set("ns", "k", "v")
    assert store.get("ns", "k") == "v"


def test_postgres_backend_degrades() -> None:
    settings = MemorySettings(
        _env_file=None,
        backend="postgres",
        postgres_dsn="",
    )
    assert isinstance(build_memory_store(settings), InMemoryMemoryStore)


def test_checkpointer_default_and_degrade() -> None:
    assert isinstance(build_checkpointer(MemorySettings(_env_file=None)), InMemorySaver)
    degraded = build_checkpointer(
        MemorySettings(
            _env_file=None,
            backend="postgres",
            postgres_dsn="postgresql://invalid",
        )
    )
    assert isinstance(degraded, InMemorySaver)


def test_key_namespace_and_prefix() -> None:
    store = InMemoryMemoryStore()
    manager = GlobalStateManager(store=store, key_prefix="cg:mem")
    manager.save(
        GlobalContractState(
            contractId="C1",
            meta={},
            activeVersionId="v1",
        )
    )
    assert store.keys("cg:mem:global") == ["C1"]


def test_unknown_version_treated_as_miss() -> None:
    store = InMemoryMemoryStore()
    store.set("ns", "k", '{"v": 999, "data": {}}')
    assert decode_model(store.get("ns", "k"), ScopedClauseMemory) is None


def test_manager_api_backward_compatible() -> None:
    manager = ScopedMemoryManager()
    manager.append(
        "c1",
        DialogTurn(role="ASSISTANT", content="旧调用", createdAt=_now()),
    )
    assert manager.get("c1").recentDialog[0].content == "旧调用"


def test_no_secret_in_store() -> None:
    memory = ScopedClauseMemory(targetClauseId="c1", draftAfter="普通草稿")
    payload = encode_model(memory)
    assert "api_key" not in payload
    assert "sk-" not in payload


def test_memory_settings_env_prefix(monkeypatch) -> None:
    monkeypatch.setenv("CG_MEMORY_TTL_S", "123")
    assert MemorySettings(_env_file=None).ttl_s == 123
