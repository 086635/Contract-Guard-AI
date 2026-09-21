"""文档解析与坐标抽取引擎。

将上传的 DOCX / PDF（文本版 / 扫描版）/ 图片解析为结构化要素、条款文本与
归一化 BBox 坐标，供后续 RAG 与审核使用。坐标体系：PDF 用户空间，72dpi，
左上原点（data_scheme.md §1.3 BBox）。

⚠️ **本文件只导出名称，不 import 引擎**（缺引擎时整包仍可导入，便于排查；
见 `3-1 §⑥-4` 与 `3-2 §④`）。
"""

from __future__ import annotations

from .config import OcrSettings, get_ocr_settings
from .errors import DocProcessorError, UnsupportedFormatError
from .router import DocRouter, RouteDecision, RouteKind

__version__ = "1.1.0"

__all__ = [
    "DocProcessorError",
    "DocRouter",
    "OcrSettings",
    "RouteDecision",
    "RouteKind",
    "UnsupportedFormatError",
    "__version__",
    "get_ocr_settings",
]
