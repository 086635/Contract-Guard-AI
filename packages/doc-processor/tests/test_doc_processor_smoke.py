"""doc-processor 冒烟自检（T-A0-4）。

本包此前**零测试**。这里只锁三件与模块 3 后续任务强相关的不变量：
1. 包可导入、版本已声明；
2. 四层契约类型**字段冻结**（3-3 / 3-5 / 3-8 的断言都建立在它之上）；
3. 包内源码不自带引擎延迟导入之外的隐患（引擎相关断言见
   `test_engine_availability.py` —— 那里才是 `3-1 §⑧` 的正式落点）。
"""

from __future__ import annotations

import dataclasses as dc


def test_package_is_importable() -> None:
    """包本体可导入且已声明版本。"""
    import doc_processor

    assert doc_processor.__version__ == "1.1.0"


def test_frozen_contracts_are_unchanged() -> None:
    """四层契约字段冻结（改动即触发模块 3 全线返工）。"""
    from doc_processor.aligner import NormalizedBBox
    from doc_processor.layout import BlockType, LayoutBlock
    from doc_processor.ocr import OcrResult, SealDetection
    from doc_processor.parser import ParsedClause, ParsedElement

    assert [f.name for f in dc.fields(LayoutBlock)] == [
        "block_type", "page", "x", "y", "width", "height", "text",
    ]
    assert [t.value for t in BlockType] == [
        "TITLE", "PARAGRAPH", "TABLE", "SEAL", "SIGNATURE", "UNKNOWN",
    ]
    assert [f.name for f in dc.fields(OcrResult)] == [
        "page", "x", "y", "width", "height", "text", "confidence",
    ]
    assert [f.name for f in dc.fields(SealDetection)] == [
        "page", "x", "y", "width", "height", "extracted_text", "confidence",
    ]
    assert [f.name for f in dc.fields(ParsedClause)] == [
        "id", "parent_id", "label", "text", "level", "children_ids",
    ]
    assert [f.name for f in dc.fields(ParsedElement)] == [
        "key", "label", "value", "confidence", "is_custom",
    ]
    assert [f.name for f in dc.fields(NormalizedBBox)][:5] == [
        "page", "x", "y", "width", "height",
    ]
