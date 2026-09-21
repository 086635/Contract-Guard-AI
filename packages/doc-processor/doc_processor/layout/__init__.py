"""版面分析模块（PP-StructureV2）。

识别文档的版面区域：标题、段落、表格、印章区、签名区，输出带页码与
BBox 的结构化区块，供 parser 进一步节点提取。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BlockType(str, Enum):
    TITLE = "TITLE"
    PARAGRAPH = "PARAGRAPH"
    TABLE = "TABLE"
    SEAL = "SEAL"
    SIGNATURE = "SIGNATURE"
    UNKNOWN = "UNKNOWN"


@dataclass
class LayoutBlock:
    block_type: BlockType
    page: int
    x: float
    y: float
    width: float
    height: float
    text: str = ""


class LayoutAnalyzer:
    """版面分析端口（契约）。

    实现见 `doc_processor.layout.analyzer.LocalLayoutAnalyzer`（T-C3，规则优先）。
    """

    def analyze(self, pdf_path: str) -> list[LayoutBlock]:
        """对 PDF 做版面分析，返回按阅读顺序排列的区块。"""
        raise NotImplementedError


# 只增导出：实现类在 analyzer.py；⚠️ `StyledLine` 是版面层**局部类型**，不在此导出
from .analyzer import LocalLayoutAnalyzer, build_layout_analyzer  # noqa: E402

__all__ = [
    "BlockType",
    "LayoutAnalyzer",
    "LayoutBlock",
    "LocalLayoutAnalyzer",
    "build_layout_analyzer",
]
