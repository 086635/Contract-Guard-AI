"""要素与条款抽取自检（T-C4 / `3-4 §⑧`）。

覆盖：
1. **正则唯一性**（全仓只有 `rag_contracts/patterns.py` 定义）与两个消费方共用同一对象；
2. 条款切分 + AST（章-条父子、children 对称、跨页仍为一条）；
3. 要素"不编造"（未命中 / 占位符 → `value=None`）、金额必须带单位；
4. 依赖方向守卫（不得 import `aligner`）与契约冻结。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from doc_processor.parser import ParsedClause, ParsedElement
from doc_processor.parser.extractor import (
    LocalNodeExtractor,
    TextLine,
    build_node_extractor,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_REPO_ROOT = Path(__file__).resolve().parents[3]

# 全仓扫描要剪枝的目录。`Path.rglob` 会一路走进 `.git` / `.tmp_t` / `.uv-cache`，
# 本机实测让下面那条「正则唯一性」守卫从 ~1s 膨胀到 **58.8s**（全仓最慢用例）。
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tmp_t",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)


def _line(text: str, *, page: int = 1, char_start: int = 0, char_end: int = 0) -> TextLine:
    """构造一行（偏移默认按文本长度推算）。"""
    return TextLine(
        page=page,
        text=text,
        x=0.0,
        y=0.0,
        width=0.0,
        height=0.0,
        char_start=char_start,
        char_end=char_end or char_start + len(text),
    )


def test_pattern_defined_once() -> None:
    """§⑦ 断言 1：条款编号字面量全仓只允许出现在 `rag_contracts/patterns.py`。"""
    # 刻意用拼接：本测试文件自身若含连续字面量，会把自己判成"第二处定义"
    needle = "第" + "[一二三四五六七八九十百千零〇"
    hits: list[str] = []
    # 用 `os.walk` + 目录剪枝（而非 `rglob`）：剪掉 `.git` / `.tmp_t` / `.uv-cache` 等
    # 与源码无关的大目录，语义不变（断言仍只认源码里的第二处定义），耗时降到 1s 级。
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            if needle in path.read_text(encoding="utf-8", errors="replace"):
                hits.append(path.name)
    assert hits == ["patterns.py"], f"正则被复制到了别处：{hits}"


def test_simple_chunker_uses_shared_pattern() -> None:
    """§⑦ 断言 2：切片器与解析器 import 的是**同一个** `re.Pattern` 对象。"""
    import rag_core.chunking.simple_chunker as chunker
    from rag_contracts.patterns import CLAUSE_HEAD_RE

    assert chunker._CLAUSE_HEAD_RE is CLAUSE_HEAD_RE


def test_clauses_from_lines_ast() -> None:
    """§⑦ 断言 4：章-条父子关系与 `children_ids` 对称。"""
    lines = [
        _line("第一章 总则", char_start=0),
        _line("第一条 甲方应付款。", char_start=7),
        _line("第二条 乙方应交付。", char_start=22),
        _line("第二章 违约责任", char_start=37),
        _line("第三条 逾期付款违约金。", char_start=47),
    ]
    clauses = LocalNodeExtractor().clauses_from_lines(lines)
    assert [clause.id for clause in clauses] == [
        "clause-0001",
        "clause-0002",
        "clause-0003",
        "clause-0004",
        "clause-0005",
    ]
    assert [clause.level for clause in clauses] == [1, 2, 2, 1, 2]
    assert clauses[0].parent_id is None
    assert clauses[0].children_ids == ["clause-0002", "clause-0003"]
    assert clauses[1].parent_id == "clause-0001"
    assert clauses[3].children_ids == ["clause-0005"]
    assert clauses[4].parent_id == "clause-0004"


def test_clause_text_includes_head_and_body() -> None:
    """条款文本 = 编号行 → 下一个编号行前一行（含编号行）。"""
    clauses = LocalNodeExtractor().clauses_from_lines(
        [
            _line("第一条 标的", char_start=0),
            _line("甲方向乙方采购服务器设备。", char_start=7),
            _line("第二条 价款", char_start=22),
        ]
    )
    assert clauses[0].text == "第一条 标的\n甲方向乙方采购服务器设备。"
    assert clauses[0].label == "第一条"
    assert clauses[1].text == "第二条 价款"


def test_clauses_across_pages() -> None:
    """跨页条款仍是**一条**（切分按行序列而非按页）。"""
    clauses = LocalNodeExtractor().clauses_from_lines(
        [
            _line("第三条 交付与验收", page=1, char_start=0),
            _line("乙方应于合同生效后六十日内交付。", page=2, char_start=10),
            _line("第四条 争议解决", page=2, char_start=30),
        ]
    )
    assert len(clauses) == 2
    assert clauses[0].text.count("\n") == 1, "跨页的两行应属同一条款"


def test_level_rules_for_dotted_and_paren() -> None:
    """编号 → 层级：`6.2`=2 / `3.1.2`=3 / `1)`=3（3-4 §⑤.3 唯一处规则）。"""
    clauses = LocalNodeExtractor().clauses_from_lines(
        [
            _line("6.2 付款方式", char_start=0),
            _line("3.1.2 逾期利息", char_start=10),
            _line("1) 附件清单", char_start=20),
        ]
    )
    assert [clause.level for clause in clauses] == [2, 3, 3]


def test_empty_lines_returns_empty() -> None:
    """空输入 → 空结果（不抛异常）。"""
    assert LocalNodeExtractor().clauses_from_lines([]) == []


def test_char_offsets_monotonic() -> None:
    """偏移非单调的行被跳过（记 warning），不影响其余条款。"""
    clauses = LocalNodeExtractor().clauses_from_lines(
        [
            _line("第一条 标的", char_start=0),
            _line("乱序行", char_start=-5),
            _line("第二条 价款", char_start=10),
        ]
    )
    assert [clause.label for clause in clauses] == ["第一条", "第二条"]


def test_build_ast_is_idempotent() -> None:
    """`build_ast()` 连跑两次结果一致（`children_ids` 先清空再回填）。"""
    extractor = LocalNodeExtractor()
    clauses = extractor.clauses_from_lines(
        [_line("第一章 总则", char_start=0), _line("第一条 标的", char_start=7)]
    )
    first = [(c.id, c.parent_id, list(c.children_ids)) for c in clauses]
    second = [(c.id, c.parent_id, list(c.children_ids)) for c in extractor.build_ast(clauses)]
    assert first == second


def test_elements_never_fabricate() -> None:
    """§⑦ 断言 5：无任何要素的文本 → 全部 `value=None` / `confidence=0.0`。"""
    elements = LocalNodeExtractor().elements_from_text("本合同一式两份。")
    assert elements, "要素表应当固定返回全部键"
    assert all(item.value is None and item.confidence == 0.0 for item in elements)
    assert all(item.is_custom is False for item in elements)


def test_amount_requires_unit() -> None:
    """§⑦ 断言 6：金额必须带单位/币种；裸数字不算金额。"""
    extractor = LocalNodeExtractor()
    hit = {
        item.key: item
        for item in extractor.elements_from_text("数量 30 个，金额人民币 12,000.00 元")
    }
    assert hit["amount"].value == "12,000.00"
    miss = {item.key: item for item in extractor.elements_from_text("编号 2024")}
    assert miss["amount"].value is None


def test_amount_upper_case_boosts_confidence() -> None:
    """数字金额与大写金额互相印证 → 置信度升到 0.95。"""
    elements = {
        item.key: item
        for item in LocalNodeExtractor().elements_from_text(
            "合同总价为人民币 1000000.00 元（大写：壹佰万元整）"
        )
    }
    assert elements["amount"].value == "1000000.00"
    assert elements["amount"].confidence == pytest.approx(0.95)


def test_placeholder_value_is_none() -> None:
    """甲方：________ → `value=None`（未填写不等于抽到了空串）。"""
    elements = {
        item.key: item for item in LocalNodeExtractor().elements_from_text("甲方：________")
    }
    assert elements["party_a"].value is None
    assert elements["party_a"].confidence == 0.0


def test_party_and_date_and_jurisdiction_extracted() -> None:
    """正常要素命中：甲乙方 / 签订日期 / 期限 / 管辖地。"""
    text = "\n".join(
        [
            "甲方：北京某某科技有限公司",
            "乙方：上海某某贸易有限公司",
            "签订日期：2026 年 1 月 1 日",
            "合同期限：自 2026 年 1 月 1 日至 2026 年 12 月 31 日",
            "争议提交北京仲裁委员会仲裁。",
        ]
    )
    elements = {item.key: item for item in LocalNodeExtractor().elements_from_text(text)}
    assert elements["party_a"].value == "北京某某科技有限公司"
    assert elements["party_b"].value == "上海某某贸易有限公司"
    assert elements["sign_date"].value == "2026 年 1 月 1 日"
    assert elements["term"].value
    assert "仲裁委员会" in (elements["jurisdiction"].value or "")


def test_extract_clauses_from_native_pdf() -> None:
    """数字版 PDF 直抽条款（走 pdfplumber，不碰 OCR）。"""
    clauses = build_node_extractor().extract_clauses(str(_FIXTURES / "native.pdf"))
    assert clauses, "夹具里每页都有第X条编号，应切出条款"
    assert all(clause.label.startswith("第") for clause in clauses)
    assert all(clause.text for clause in clauses)


def test_extract_elements_from_docx_fixture() -> None:
    """DOCX 快捷路径：要素抽取可用（页数限制在前 2 页内无影响）。"""
    elements = {
        item.key: item
        for item in build_node_extractor().extract_elements(str(_FIXTURES / "contract.docx"))
    }
    assert elements["party_a"].value == "北京某某科技有限公司"
    assert elements["party_b"].value == "上海某某贸易有限公司"


def test_no_aligner_import() -> None:
    """依赖方向守卫：`extractor` 不得 import `aligner`（避免循环导入）。"""
    source = (
        Path(__file__).resolve().parents[1] / "doc_processor" / "parser" / "extractor.py"
    ).read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert not [line for line in imports if "aligner" in line], imports


def test_contracts_unchanged() -> None:
    """§⑦ 断言 7：`ParsedClause` / `ParsedElement` 字段冻结。"""
    import dataclasses as dc

    assert [field.name for field in dc.fields(ParsedClause)] == [
        "id",
        "parent_id",
        "label",
        "text",
        "level",
        "children_ids",
    ]
    assert [field.name for field in dc.fields(ParsedElement)] == [
        "key",
        "label",
        "value",
        "confidence",
        "is_custom",
    ]


# ---------------------------------------------------------------------------
# C80 / T-D8-5：解析层 key 必须与要素词表唯一处一致（防漂移守卫）
# ---------------------------------------------------------------------------
def test_element_rule_keys_match_vocabulary() -> None:
    """解析层 `_ELEMENT_RULES` 的 key 与 `rag_contracts.element_keys` 完全一致。"""
    from doc_processor.parser.extractor import _ELEMENT_RULES, _VALUE_GROUP
    from rag_contracts.element_keys import CORE_ELEMENT_LABELS

    rule_keys = {key for key, *_ in _ELEMENT_RULES}
    assert rule_keys == set(CORE_ELEMENT_LABELS)
    assert set(_VALUE_GROUP) == set(CORE_ELEMENT_LABELS)


def test_element_rule_labels_come_from_vocabulary() -> None:
    """标签也取自词表（解析层不得再维护第二份 label）。"""
    from doc_processor.parser.extractor import _ELEMENT_RULES
    from rag_contracts.element_keys import CORE_ELEMENT_LABELS

    for key, label, *_ in _ELEMENT_RULES:
        assert label == CORE_ELEMENT_LABELS[key]
