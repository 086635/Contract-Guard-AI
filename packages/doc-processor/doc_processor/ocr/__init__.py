"""OCR 与印章定位模块（PaddleOCR）。

对扫描版 PDF / 图片做文字 OCR，并定位印章区域、提取印章文字（公司名 / 税号），
输出 SealSignatureDetection 所需的 bbox / extractedText / confidence。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OcrResult:
    page: int
    x: float
    y: float
    width: float
    height: float
    text: str
    confidence: float


@dataclass
class SealDetection:
    page: int
    x: float
    y: float
    width: float
    height: float
    extracted_text: str | None
    confidence: float


class OcrEngine:
    """OCR 引擎端口（契约，**字段与方法签名冻结**）。

    实现见 `doc_processor.ocr.engine.LocalOcrEngine`（T-C2）。
    """

    def ocr_page(self, image_path: str) -> list[OcrResult]:
        """对单张图片做 OCR。"""
        raise NotImplementedError

    def detect_seals(self, image_path: str) -> list[SealDetection]:
        """检测印章（含印章文字识别）。"""
        raise NotImplementedError


# 只增导出：实现类在 engine.py（引擎 import 全部延迟到函数体内，缺引擎不影响本包导入）
from .engine import LocalOcrEngine, build_ocr_engine, detect_engine  # noqa: E402

__all__ = [
    "LocalOcrEngine",
    "OcrEngine",
    "OcrResult",
    "SealDetection",
    "build_ocr_engine",
    "detect_engine",
]
