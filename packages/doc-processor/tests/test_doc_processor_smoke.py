"""doc-processor 冒烟自检（T-A0-4）。

本包此前**零测试**。这里只锁三件与模块 3 后续任务强相关的不变量：
1. 包可导入、版本已声明；
2. 四层契约类型**字段冻结**（3-3 / 3-5 / 3-8 的断言都建立在它之上）；
3. 引擎不得在模块顶层导入（`3-1 §⑧` 的延迟导入红线，保证离线可导入）。
"""

from __future__ import annotations

import dataclasses as dc
from pathlib import Path


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


def test_engine_not_imported_at_package_top_level() -> None:
    """`3-1 §⑧`：引擎只能在函数体内延迟导入，模块顶层出现即破坏离线可导入性。"""
    root = Path(__file__).resolve().parents[1] / "doc_processor"
    banned = (
        "import paddleocr",
        "from paddleocr",
        "import rapidocr_onnxruntime",
        "from rapidocr_onnxruntime",
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(
            open(path, encoding="utf-8").read().splitlines(), start=1
        ):
            if line.startswith((" ", "\t")):  # 缩进 = 函数体内，属允许的延迟导入
                continue
            if any(token in line for token in banned):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], f"引擎必须延迟导入：{offenders}"
