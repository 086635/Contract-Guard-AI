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
    """要素 / 条款节点提取端口（契约，**字段与方法签名冻结**）。

    实现见 `doc_processor.parser.extractor.LocalNodeExtractor`（T-C4，规则优先）。
    """

    def extract_elements(self, source_path: str) -> list[ParsedElement]:
        """抽取合同要素（甲乙方 / 金额 / 日期 / 期限 / 管辖地）。"""
        raise NotImplementedError

    def extract_clauses(self, source_path: str) -> list[ParsedClause]:
        """抽取条款并构建 AST 拓扑（level / parent_id / children_ids）。"""
        raise NotImplementedError


# 只增导出：实现类在 extractor.py；`TextLine` 供 3-5 / 3-6 复用（单向依赖，见 3-4 §③）
from .extractor import LocalNodeExtractor, TextLine, build_node_extractor  # noqa: E402

__all__ = [
    "LocalNodeExtractor",
    "NodeExtractor",
    "ParsedClause",
    "ParsedElement",
    "TextLine",
    "build_node_extractor",
]
