"""节点提取模块（pdfplumber / python-docx）。

从版面分析结果与 OCR 结果中抽取：合同要素（ContractElement）、条款节点
（ClauseNode，构建 AST 拓扑）、表格键值对。文本版 PDF 走 pdfplumber，
DOCX 走 python-docx。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedElement:
    key: str
    label: str
    value: str | None = None
    confidence: float = 0.0
    is_custom: bool = False


@dataclass
class ParsedClause:
    id: str
    parent_id: str | None = None
    label: str = ""
    text: str = ""
    level: int = 0
    children_ids: list[str] = field(default_factory=list)


class NodeExtractor:
    """要素 / 条款节点提取骨架。

    TODO（工程实现）：结合 layout + ocr 输出，抽取 ParsedElement 与
    ParsedClause（AST 拓扑），归一化到 BBox 坐标。
    """

    def extract_elements(self, source_path: str) -> list[ParsedElement]:
        raise NotImplementedError("parser.extract_elements 待实现")

    def extract_clauses(self, source_path: str) -> list[ParsedClause]:
        raise NotImplementedError("parser.extract_clauses 待实现")
