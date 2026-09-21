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
    """坐标归一化对齐端口（契约）。

    `to_pdf_points()` **已实现且语义正确 —— 全仓唯一缩放处，禁止另写一份**。
    `(char_offset → BBox)` 索引表由子类实现（见 `aligner/aligner.py`，T-C5）。
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


# 只增导出：索引器实现（`(char_offset → BBox)`）在 aligner.py；
# `NormalizedBBox` 与 `to_pdf_points()` 一字未改。
from .aligner import LocalBBoxAligner, build_bbox_aligner  # noqa: E402

__all__ = [
    "BBoxAligner",
    "LocalBBoxAligner",
    "NormalizedBBox",
    "build_bbox_aligner",
]
