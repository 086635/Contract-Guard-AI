"""版面分析器自检（T-C3 / `3-7 §⑧`）。

覆盖：
1. 规则表逐条（字号标题 / 加粗标题 / 长行不可能是标题 / 表格 / 签名区 / 印章关键词）；
2. 归并规则（同类相邻合并、空行断块、间距阈值、单例类型独立成块）；
3. PDF 集成（`native.pdf` 出块且首块是标题）与扫描件兜底（**不返回空**）；
4. 三条红线：无二次缩放 / 无行聚类 / `StyledLine` 不进包出口。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_processor.errors import UnsupportedFormatError
from doc_processor.layout import BlockType, LayoutAnalyzer, LayoutBlock
from doc_processor.layout.analyzer import (
    BODY_SIZE_FALLBACK,
    LocalLayoutAnalyzer,
    StyledLine,
    build_layout_analyzer,
    classify_line,
    merge_blocks,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_A4_HEIGHT = 842.0


def _line(
    text: str = "正文",
    *,
    y: float = 0.0,
    height: float = 10.0,
    size: float = BODY_SIZE_FALLBACK,
    bold: bool = False,
    in_table: bool = False,
    x: float = 0.0,
    width: float = 200.0,
    page: int = 1,
) -> StyledLine:
    """构造一行（默认是"普通正文"）。"""
    return StyledLine(
        page=page,
        text=text,
        x=x,
        y=y,
        width=width,
        height=height,
        size=size,
        bold=bold,
        in_table=in_table,
    )


# ===========================================================================
# 1. 规则表
# ===========================================================================
def test_title_by_size() -> None:
    """字号 ≥ 正文字号 × 1.2 → TITLE。"""
    title = _line("PURCHASE CONTRACT", size=20.0, height=20.0)
    body = _line("Article 1 ...", y=40.0, size=10.0, height=12.0)
    assert classify_line(title, body_size=10.0) is BlockType.TITLE
    assert classify_line(body, body_size=10.0) is BlockType.PARAGRAPH


def test_title_by_bold() -> None:
    """加粗但字号正常 → 仍是 TITLE。"""
    assert classify_line(_line("加粗小标题", bold=True), body_size=10.5) is BlockType.TITLE


def test_long_line_never_title() -> None:
    """加粗 + 超长 → PARAGRAPH（`TITLE_MAX_CHARS` 生效）。"""
    long_text = "加粗的长正文" * 15
    assert len(long_text) > 40
    assert classify_line(_line(long_text, bold=True), body_size=10.5) is BlockType.PARAGRAPH


def test_table_by_box_intersection() -> None:
    """与表格框相交 → TABLE（`in_table` 或外部传入 `table_boxes` 都算）。"""
    line = _line("表格内文字", y=100.0)
    assert classify_line(line, body_size=10.5, table_boxes=[(0.0, 90.0, 300.0, 130.0)]) is BlockType.TABLE
    assert classify_line(_line("表格内文字", in_table=True), body_size=10.5) is BlockType.TABLE


def test_signature_requires_footer_zone() -> None:
    """同一句签署文字：在下 1/3 → SIGNATURE；在页面上方 → 不是。"""
    footer_line = _line("甲方（签字）：", y=_A4_HEIGHT * 0.8)
    top_line = _line("甲方（签字）：", y=100.0)
    assert classify_line(footer_line, body_size=10.5, page_height=_A4_HEIGHT) is BlockType.SIGNATURE
    assert classify_line(top_line, body_size=10.5, page_height=_A4_HEIGHT) is not BlockType.SIGNATURE


def test_signature_without_page_height_skipped() -> None:
    """`page_height=None` → 跳过签名区规则（宁可漏判，也不误判正文里的"甲方"引用）。"""
    line = _line("甲方（签字）：", y=800.0)
    assert classify_line(line, body_size=10.5, page_height=None) is not BlockType.SIGNATURE


def test_seal_keyword() -> None:
    """含印章关键词 → SEAL。"""
    assert classify_line(_line("此处加盖公章"), body_size=10.5) is BlockType.SEAL
    assert classify_line(_line("（印章）"), body_size=10.5) is BlockType.SEAL


def test_unknown_when_no_text() -> None:
    """空行 → UNKNOWN（由归并阶段丢弃）。"""
    assert classify_line(_line("   "), body_size=10.5) is BlockType.UNKNOWN


# ===========================================================================
# 2. 归并规则
# ===========================================================================
def test_blank_line_breaks_block() -> None:
    """空行断块，且**不产出**空块。"""
    blocks = merge_blocks(
        [_line("a", y=0.0), _line("b", y=11.0), _line("", y=22.0), _line("c", y=40.0)],
        page=1,
    )
    assert len(blocks) == 2
    assert blocks[0].text == "a\nb"
    assert blocks[1].text == "c"


def test_merge_gap_threshold() -> None:
    """间距 ≤ 行高 × `MERGE_GAP_RATIO` → 合并；明显超过 → 断开。

    阈值口径：`gap = 本行 y - (上一行 y + 上一行 height)`，行高 10 → 阈值为 18。
    """
    merged = merge_blocks([_line("a", y=0.0, height=10.0), _line("b", y=18.0, height=10.0)], page=1)
    assert len(merged) == 1, "间距 8 ≤ 18 → 应合并"
    split = merge_blocks([_line("a", y=0.0, height=10.0), _line("b", y=40.0, height=10.0)], page=1)
    assert len(split) == 2, "间距 30 > 18 → 应断开"


def test_merge_gap_threshold_exact() -> None:
    """间距阈值精确边界：≤ 行高×1.8 合并，> 则断开。"""
    same = merge_blocks([_line("a", y=0.0, height=10.0), _line("b", y=28.0, height=10.0)], page=1)
    assert len(same) == 1  # 间距 18 == 10×1.8 → 合并
    split = merge_blocks([_line("a", y=0.0, height=10.0), _line("b", y=29.0, height=10.0)], page=1)
    assert len(split) == 2  # 间距 19 > 18 → 断开


def test_singleton_types_not_merged() -> None:
    """相邻两个 TITLE（或 SEAL / SIGNATURE）各自独立成块。"""
    # 显式给 body_size：否则两行等大时"中位数"就是 20，按规则判定不出 TITLE
    blocks = merge_blocks(
        [_line("标题一", y=0.0, height=10.0, size=20.0), _line("标题二", y=11.0, height=10.0, size=20.0)],
        page=1,
        body_size=10.5,
    )
    assert len(blocks) == 2
    assert all(block.block_type is BlockType.TITLE for block in blocks)


def test_merge_uses_union_bbox() -> None:
    """块 bbox = 各行框并集；text 用 `"\n"` 拼接（与 3-6 全文分隔符一致）。"""
    blocks = merge_blocks(
        [
            _line("a", x=10.0, y=0.0, width=100.0, height=10.0),
            _line("b", x=5.0, y=11.0, width=50.0, height=10.0),
        ],
        page=1,
    )
    assert len(blocks) == 1
    block = blocks[0]
    assert (block.x, block.y) == (5.0, 0.0)
    assert (block.width, block.height) == (105.0, 21.0)
    assert block.text == "a\nb"


def test_empty_lines_returns_empty() -> None:
    """全空行 → 空结果（不抛异常、不产出空块）。"""
    assert merge_blocks([_line(""), _line("  ")], page=1) == []


def test_body_size_use_median_not_mean() -> None:
    """正文字号取**中位数**：一页里混入一个超大标题不应拉高正文基准。"""
    lines = [_line("正文一", y=0.0, size=10.0), _line("正文二", y=11.0, size=10.0), _line("大标题", y=22.0, size=40.0)]
    blocks = LocalLayoutAnalyzer().analyze_lines(lines, page=1)
    assert blocks[-1].block_type is BlockType.TITLE
    assert blocks[0].block_type is BlockType.PARAGRAPH


# ===========================================================================
# 3. 集成与兜底
# ===========================================================================
def test_analyze_demo_pdf() -> None:
    """§⑦ 断言 2：数字版 PDF 出块，首块是 TITLE，且存在 PARAGRAPH。"""
    blocks = build_layout_analyzer().analyze(str(_FIXTURES / "native.pdf"))
    assert blocks
    assert blocks[0].block_type is BlockType.TITLE
    assert any(block.block_type is BlockType.PARAGRAPH for block in blocks)
    assert max(block.page for block in blocks) == 5


def test_scanned_fallback_not_empty() -> None:
    """扫描件（无字号信息）→ 仍返回占位块，**不返回空**（前端要有东西可画）。"""
    blocks = build_layout_analyzer().analyze(str(_FIXTURES / "scanned.pdf"))
    assert blocks, "扫描件兜底不得返回空"
    assert all(block.block_type is BlockType.PARAGRAPH for block in blocks)
    assert all(block.width > 0 and block.height > 0 for block in blocks)


def test_scanned_fallback_uses_real_page_size() -> None:
    """扫描件占位块用 PDF 真实页尺寸（A4 ≈ 595×842 点）。"""
    block = build_layout_analyzer().analyze(str(_FIXTURES / "scanned.pdf"))[0]
    assert block.width == pytest.approx(595.0, abs=2.0)
    assert block.height == pytest.approx(842.0, abs=2.0)


def test_image_fallback_returns_placeholder() -> None:
    """图片 → A4 占位块（本模块不做像素→点换算）。"""
    blocks = build_layout_analyzer().analyze(str(_FIXTURES / "scan.png"))
    assert len(blocks) == 1
    assert blocks[0].block_type is BlockType.PARAGRAPH


def test_docx_raises_unsupported() -> None:
    """DOCX 不做版面分析（其版面=段落样式，属 3-4 直抽路径）→ 明确报错。"""
    with pytest.raises(UnsupportedFormatError):
        build_layout_analyzer().analyze(str(_FIXTURES / "contract.docx"))


def test_missing_file_raises_unsupported(tmp_path: Path) -> None:
    """不存在的路径 → `UnsupportedFormatError`。"""
    with pytest.raises(UnsupportedFormatError):
        build_layout_analyzer().analyze(str(tmp_path / "nope.pdf"))


def test_all_fallback_size_still_returns_block() -> None:
    """字号全缺（全 `BODY_SIZE_FALLBACK`）时仍返回 ≥ 1 块。"""
    lines = [_line("第一行", y=0.0), _line("第二行", y=11.0)]
    assert len(LocalLayoutAnalyzer().analyze_lines(lines, page=1)) == 1


def test_factory_returns_analyzer_port() -> None:
    """工厂返回 `LayoutAnalyzer` 实现（与 3-6 的接线约定）。"""
    assert isinstance(build_layout_analyzer(), LayoutAnalyzer)
    assert isinstance(build_layout_analyzer(), LocalLayoutAnalyzer)


# ===========================================================================
# 4. 三条红线
# ===========================================================================
def test_no_scaling_no_clustering() -> None:
    """§⑦ 断言 5：本模块不得出现二次缩放，也不得自己写行聚类。"""
    source = (Path(__file__).resolve().parents[1] / "doc_processor" / "layout" / "analyzer.py").read_text(
        encoding="utf-8"
    )
    for banned in ("72 /", "72.0 /", "src_dpi", "extract_words"):
        assert banned not in source, f"禁止出现 {banned}"


def test_contracts_unchanged() -> None:
    """§⑦ 断言 7：`LayoutBlock` / `BlockType` 冻结。"""
    import dataclasses as dc

    assert [field.name for field in dc.fields(LayoutBlock)] == [
        "block_type",
        "page",
        "x",
        "y",
        "width",
        "height",
        "text",
    ]
    assert [item.value for item in BlockType] == [
        "TITLE",
        "PARAGRAPH",
        "TABLE",
        "SEAL",
        "SIGNATURE",
        "UNKNOWN",
    ]


def test_styled_line_not_exported() -> None:
    """`StyledLine` 是版面层局部类型，**不进包出口**（否则会变成第二套行契约）。"""
    import doc_processor.layout as layout_pkg

    assert "StyledLine" not in layout_pkg.__all__
    with pytest.raises(AttributeError):
        getattr(layout_pkg, "StyledLine")
