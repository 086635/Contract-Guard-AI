"""条款编号正则的语义回归（T-C4 / `3-4 §⑧`）。

迁移纪律：期望值**写死**在本文件里 —— 迁移只允许换"定义位置"，语义变一点都算回归。
"""

from __future__ import annotations

import pytest

from rag_contracts.patterns import CLAUSE_HEAD_RE

_CHINESE_CASES = [
    ("第五条 价款与支付", "第五条", "价款与支付"),
    ("第五章 总则", "第五章", "总则"),
    ("第3章 交付与验收", "第3章", "交付与验收"),
    ("第五百八十五条 责任的限制", "第五百八十五条", "责任的限制"),
    ("  第七条  保密义务", "第七条", "保密义务"),
]

_ARABIC_CASES = [
    ("6.2 付款方式", "6.2", "付款方式"),
    ("3.1.2 逾期利息", "3.1.2", "逾期利息"),
    ("1) 附件清单", "1)", "附件清单"),
    ("1. 这是一级列表但缺右括号", "1.", "这是一级列表但缺右括号"),
]


@pytest.mark.parametrize(("text", "num", "rest"), _CHINESE_CASES)
def test_clause_head_matches_chinese_numbers(text: str, num: str, rest: str) -> None:
    """中文编号：第X条 / 第X章 / 第3章 / 第X百X十X条（含前后空白容忍）。"""
    match = CLAUSE_HEAD_RE.match(text)
    assert match is not None, text
    assert match.group("num") == num
    assert match.group("rest").strip() == rest


@pytest.mark.parametrize(("text", "num", "rest"), _ARABIC_CASES)
def test_clause_head_matches_arabic_and_dotted(text: str, num: str, rest: str) -> None:
    """阿拉伯编号：6.2 / 3.1.2 / 1)。"""
    match = CLAUSE_HEAD_RE.match(text)
    assert match is not None, text
    assert match.group("num") == num
    assert match.group("rest") == rest


@pytest.mark.parametrize(
    "text",
    [
        "普通正文，没有任何编号",
        "甲方向乙方采购服务器设备共计一百台。",
        "第X条 用字母编号不算",
    ],
)
def test_clause_head_rejects_plain_text(text: str) -> None:
    """普通正文 / 非法编号形式不得被误判为条款头。"""
    assert CLAUSE_HEAD_RE.match(text) is None
