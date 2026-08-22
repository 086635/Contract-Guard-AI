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
    """PaddleOCR + 印章定位骨架。

    TODO（工程实现）：接入 PaddleOCR 与印章检测模型，输出 OcrResult / SealDetection。
    """

    def ocr_page(self, image_path: str) -> list[OcrResult]:
        raise NotImplementedError("ocr.ocr_page 待实现：接入 PaddleOCR")

    def detect_seals(self, image_path: str) -> list[SealDetection]:
        raise NotImplementedError("ocr.detect_seals 待实现：接入印章检测模型")
