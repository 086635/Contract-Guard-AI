"""Common contract numbering uses the shared parser/RAG anchor."""
import pytest

from doc_processor.parser.extractor import LocalNodeExtractor, TextLine
from rag_contracts.patterns import CLAUSE_HEAD_RE


@pytest.mark.parametrize("head", ["一、", "二．", "1.", "2、", "（一）", "(2)", "第三条", "1.1", "3.1.2"])
def test_numbered_clause_is_extracted(head: str) -> None:
    text = f"{head}付款与交付"
    match = CLAUSE_HEAD_RE.match(text)
    assert match and match.group("num") == head
    clauses = LocalNodeExtractor().clauses_from_lines([TextLine(1, text, 10, 20, 200, 15, 0, len(text))])
    assert len(clauses) == 1
    assert clauses[0].label == head
    assert clauses[0].text == text

@pytest.mark.parametrize("text", ["2026-10-05", "2026年10月5日", "320000元", "付款金额320000.00元", "合同主体"])
def test_body_text_is_not_invented_as_clause(text: str) -> None:
    assert CLAUSE_HEAD_RE.match(text) is None
