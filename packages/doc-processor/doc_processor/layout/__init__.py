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
    """PP-StructureV2 版面分析骨架。

    TODO（工程实现）：接入 PP-StructureV2，输出 LayoutBlock 列表。
    """

    def analyze(self, pdf_path: str) -> list[LayoutBlock]:
        raise NotImplementedError("layout.analyze 待实现：接入 PP-StructureV2")
