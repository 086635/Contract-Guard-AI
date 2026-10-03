"""C80 / T-D8-5：要素 key 词表（唯一处）契约测试。"""

from __future__ import annotations

from rag_contracts.element_keys import (
    ALLOWED_ELEMENT_KEYS,
    CORE_ELEMENT_KEYS,
    CORE_ELEMENT_LABELS,
    DETAIL_ELEMENT_KEYS,
    ELEMENT_KEY_ALIASES,
    ELEMENT_KEYS_PROMPT_BLOCK,
    is_allowed_key,
    is_core_key,
    is_detail_key,
    label_for,
    normalize_key,
)


def test_core_vocabulary_is_six_keys_with_labels() -> None:
    """核心词表固定 6 项且每项都有非空中文标签（赛题「关键要素」集合）。"""
    assert len(CORE_ELEMENT_KEYS) == 6
    assert [key for key, _ in CORE_ELEMENT_KEYS] == [
        "party_a",
        "party_b",
        "amount",
        "sign_date",
        "term",
        "jurisdiction",
    ]
    assert all(label.strip() for _, label in CORE_ELEMENT_KEYS)


def test_detail_whitelist_and_no_duplicate_keys() -> None:
    """明细白名单与核心词表不得重复定义同一个 key。"""
    core = {key for key, _ in CORE_ELEMENT_KEYS}
    detail = {key for key, _ in DETAIL_ELEMENT_KEYS}
    assert detail and core.isdisjoint(detail)
    assert ALLOWED_ELEMENT_KEYS == core | detail


def test_alias_normalizes_to_core_and_avoids_duplicate_concept() -> None:
    """别名归一到核心 key，且别名不得再出现在核心/明细词表里（同概念只允许一处）。"""
    assert normalize_key(" contract_amount ") == "amount"
    assert is_core_key("contract_amount") is True
    assert "contract_amount" not in ALLOWED_ELEMENT_KEYS
    for alias, target in ELEMENT_KEY_ALIASES.items():
        assert alias not in ALLOWED_ELEMENT_KEYS
        assert target in ALLOWED_ELEMENT_KEYS


def test_key_classification_helpers() -> None:
    """核心 / 明细 / 越界三类判定必须互斥且可判定。"""
    assert is_core_key("amount") is True
    assert is_detail_key("amount") is False
    assert is_detail_key("payment_method") is True
    assert is_core_key("payment_method") is False
    assert is_allowed_key("payment_method") is True
    assert is_allowed_key("invented_key_xyz") is False
    assert normalize_key(None) == ""
    assert is_core_key("") is False


def test_label_lookup_and_prompt_block() -> None:
    """标签查询与提示词注入块都来自同一词表（模板不得硬编码 key）。"""
    assert label_for("party_a") == CORE_ELEMENT_LABELS["party_a"]
    assert label_for("payment_method") == "付款方式"
    assert label_for("unknown", default="其它") == "其它"

    block = ELEMENT_KEYS_PROMPT_BLOCK
    for key in ALLOWED_ELEMENT_KEYS:
        assert key in block
    assert "不得发明新 key" in block
    assert "is_custom=false" in block
