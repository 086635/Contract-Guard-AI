"""坐标与偏移索引自检（T-C5 / `3-5 §⑧`）。

覆盖：
1. 区间语义（半开区间、相邻不重叠、越界裁剪、非法区间）；
2. 查找与降级（跨行多框、跨页两框、去重、未索引返回空且只告警一次）；
3. 索引纪律（排序副本不改入参、非法行跳过、二分查找的性能底线）；
4. 红线（本文件不得出现二次缩放、`NormalizedBBox` 字段冻结）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from doc_processor.aligner import (
    BBoxAligner,
    LocalBBoxAligner,
    NormalizedBBox,
    build_bbox_aligner,
)
from doc_processor.parser.extractor import TextLine


def _line(
    text: str = "abc",
    *,
    page: int = 1,
    x: float = 10.0,
    y: float = 10.0,
    width: float = 30.0,
    height: float = 10.0,
    char_start: int = 0,
    char_end: int | None = None,
) -> TextLine:
    """构造一行（`char_end` 缺省按文本长度推算）。"""
    return TextLine(
        page=page,
        text=text,
        x=x,
        y=y,
        width=width,
        height=height,
        char_start=char_start,
        char_end=char_start + len(text) if char_end is None else char_end,
    )


def _three_lines() -> list[TextLine]:
    """三行连续文本（`abc`/`def`/`ghi`，偏移 0-3 / 3-6 / 6-9）。"""
    return [
        _line("abc", y=10.0, char_start=0),
        _line("def", y=20.0, char_start=3),
        _line("ghi", y=30.0, char_start=6),
    ]


def test_factory_returns_port_implementation() -> None:
    """工厂返回实现了端口的本机实现。"""
    aligner = build_bbox_aligner()
    assert isinstance(aligner, LocalBBoxAligner)
    assert isinstance(aligner, BBoxAligner)


def test_locate_across_lines() -> None:
    """跨行区间 → 多框，且按阅读顺序返回。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(_three_lines())
    boxes = aligner.locate(2, 6)  # [2,6)：命中第 1、2 行
    assert [box.y for box in boxes] == [10.0, 20.0]
    assert all(box.coordinate_system == "PDF_POINTS" for box in boxes)


def test_half_open_interval_boundaries() -> None:
    """半开区间：`[start, end)` —— 相邻区间不重叠，空区间返回空。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets([_line("abc", char_start=0), _line("def", y=20.0, char_start=3)])
    assert aligner.locate(0, 0) == []
    assert len(aligner.locate(0, 3)) == 1
    assert len(aligner.locate(3, 6)) == 1
    assert len(aligner.locate(0, 6)) == 2
    assert aligner.total_chars == 6


def test_clamp_and_invalid_range() -> None:
    """越界自动裁剪；`start >= end` 返回空。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(_three_lines())
    assert len(aligner.locate(-10, 999)) == 3
    assert aligner.locate(5, 2) == []


def test_unindexed_returns_empty(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """未索引 → 空结果 + **只告警一次**（避免前端初始化期刷屏）。"""
    aligner = build_bbox_aligner()
    monkeypatch.setattr(logging.getLogger("doc_processor.aligner.aligner"), "propagate", True)
    with caplog.at_level("WARNING", logger="doc_processor.aligner.aligner"):
        assert aligner.locate(0, 10) == []
        assert aligner.locate(0, 10) == []
    assert aligner.indexed is False
    assert caplog.text.count("偏移索引尚未建立") == 1


def test_dedup_identical_boxes() -> None:
    """同名坐标的不同行只保留一个框。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(
        [
            _line("abc", x=10.0, y=10.0, char_start=0),
            _line("def", x=10.0, y=10.0, char_start=3),
        ]
    )
    assert len(aligner.locate(0, 6)) == 1


def test_cross_page_returns_two_boxes() -> None:
    """两页各一行覆盖同一区间 → 2 个框且页码不同。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(
        [
            _line("abc", page=1, char_start=0),
            _line("def", page=2, char_start=3),
        ]
    )
    boxes = aligner.locate(2, 4)
    assert len(boxes) == 2
    assert {box.page for box in boxes} == {1, 2}


def test_index_is_sorted_copy() -> None:
    """乱序输入：内部排序，**不改动调用方列表**。"""
    aligner = build_bbox_aligner()
    lines = _three_lines()
    shuffled = [lines[2], lines[0], lines[1]]
    snapshot = list(shuffled)
    aligner.index_text_offsets(shuffled)
    assert [line.char_start for line in shuffled] == [line.char_start for line in snapshot]
    assert [box.y for box in aligner.locate(0, 9)] == [10.0, 20.0, 30.0]


def test_invalid_line_skipped(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """非法偏移（`char_end < char_start`）→ 跳过该行 + warning，不影响其余行。"""
    aligner = build_bbox_aligner()
    monkeypatch.setattr(logging.getLogger("doc_processor.aligner.aligner"), "propagate", True)
    with caplog.at_level("WARNING", logger="doc_processor.aligner.aligner"):
        aligner.index_text_offsets(
            [
                _line("abc", char_start=0),
                _line("bad", y=20.0, char_start=10, char_end=1),
            ]
        )
    assert len(aligner.locate(0, 3)) == 1
    assert "偏移非法的行" in caplog.text


def test_empty_index_returns_empty() -> None:
    """索引建了但没有任何行 → `indexed` 为 True（已建索引）、`locate()` 仍返回空。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets([])
    assert aligner.indexed is True
    assert aligner.total_chars == 0
    assert aligner.locate(0, 10) == []


def test_coordinates_rounded_to_one_decimal() -> None:
    """坐标四舍五入到 0.1 pt（前端 Canvas 精度约定）。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets([_line("abc", x=10.06, y=20.04, width=30.05, height=9.96, char_start=0)])
    box = aligner.locate(0, 3)[0]
    assert (box.x, box.y, box.width, box.height) == (10.1, 20.0, 30.1, 10.0)


def test_locate_is_fast() -> None:
    """复杂度底线：10000 行 + 1000 次 `locate()` 必须远快于线性扫描。"""
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(
        [_line("abcdefghij", y=float(index), char_start=index * 10) for index in range(10_000)]
    )
    started = time.perf_counter()
    for index in range(1000):
        aligner.locate(index * 10, index * 10 + 15)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"locate() 过慢（{elapsed:.3f}s），疑似退化为线性扫描"


def test_no_scaling_in_aligner() -> None:
    """红线：本文件不得出现二次缩放（`to_pdf_points()` 是唯一处）。"""
    source = (
        Path(__file__).resolve().parents[1] / "doc_processor" / "aligner" / "aligner.py"
    ).read_text(encoding="utf-8")
    for banned in ("72 /", "72.0 /", "src_dpi"):
        assert banned not in source, f"禁止出现 {banned}"


def test_contracts_unchanged() -> None:
    """`NormalizedBBox` 字段冻结（含坐标体系常量）。"""
    import dataclasses as dc

    assert [field.name for field in dc.fields(NormalizedBBox)][:5] == [
        "page",
        "x",
        "y",
        "width",
        "height",
    ]
    assert NormalizedBBox(1, 0.0, 0.0, 0.0, 0.0).coordinate_system == "PDF_POINTS"


def test_to_pdf_points_still_the_single_scaling_place() -> None:
    """`to_pdf_points()` 语义未变（300dpi 下 300x60 px → 72x14.4 pt）。"""
    box = BBoxAligner().to_pdf_points(1, 300, 600, 300, 60, src_dpi=300)
    assert (box.x, box.y) == (72.0, 144.0)
    assert (box.width, box.height) == pytest.approx((72.0, 14.4))
