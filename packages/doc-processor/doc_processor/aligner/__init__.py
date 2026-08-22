"""对齐器（aligner）。

将不同来源的坐标（pdfplumber 原生 / OCR 像素 / python-docx 偏移）归一化到
统一的 BBox（PDF 用户空间，72dpi，左上原点），并建立文本 Offset 与 BBox 的
映射，供前端 PDF.js Canvas 高亮 Overlay 精确命中。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NormalizedBBox:
    page: int
    x: float
    y: float
    width: float
    height: float
    coordinate_system: str = "PDF_POINTS"


class BBoxAligner:
    """坐标归一化对齐骨架。

    TODO（工程实现）：实现多源坐标系到 PDF_POINTS 的仿射变换，并维护
    (char_offset -> BBox) 索引表。
    """

    def to_pdf_points(
        self, page: int, x: float, y: float, w: float, h: float, src_dpi: int = 72
    ) -> NormalizedBBox:
        scale = 72.0 / src_dpi
        return NormalizedBBox(
            page=page,
            x=x * scale,
            y=y * scale,
            width=w * scale,
            height=h * scale,
        )
