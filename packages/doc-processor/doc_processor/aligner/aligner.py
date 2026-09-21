"""坐标与偏移索引（T-C5 / `3-5`）：把字符区间换算成 BBox 列表。

为什么它是前端高亮的前提：前端拿到的是「第 N 到第 M 个字符」，
**必须**靠这张索引换算出 BBox 才能画高亮框；`ContractChunk.bbox` 是**整条条款**的框，
两者不可互换（模块总纲 §3.4.2）。

三条纪律：
1. **二分查找**：前端每次悬停/滚动都会调用，禁止线性扫描。
2. **不重写缩放**：本模块拿到的坐标已是 PDF_POINTS，缩放唯一处是 `to_pdf_points()`。
3. **永不抛异常**：未索引 / 区间非法 / 越界 → 返回 `[]`（前端初始化时序不保证）。
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from cg_observability import ensure_trace_id, get_logger, log_event

from ..parser.extractor import TextLine
from . import BBoxAligner, NormalizedBBox

_LOG = get_logger("doc_processor.aligner.aligner")

#: 坐标精度（pt）：前端 Canvas 无需更高精度（3-5 §⑥-9）
_PRECISION = 0.1


class LocalBBoxAligner(BBoxAligner):
    """字符偏移 ↔ BBox 索引器。

    设计要点：
    1. **半开区间**：`locate(start, end)` 覆盖 `[start, end)`（与前端 `selection` 语义一致）。
    2. **二分查找**：索引按 `char_start` 升序，定位用 `bisect`，复杂度 `O(log n + k)`。
    3. **永不抛异常**：未索引 / 区间非法 / 越界 → 返回 `[]` 或裁剪后的结果。
    """

    def __init__(self) -> None:
        """初始化空索引。"""
        self._lines: list[TextLine] = []
        self._starts: list[int] = []
        self._total_chars = 0
        self._indexed = False
        self._unindexed_warned = False

    # ---------------- 建索引 ----------------
    def index_text_offsets(self, lines: Sequence[TextLine]) -> None:
        """建立索引（幂等；重复调用以最后一次为准）。

        Args:
            lines: 行序列（顺序任意，内部按 `char_start` 排序副本）。
        """
        valid: list[TextLine] = []
        for line in lines:
            if line.char_end < line.char_start:
                _LOG.warning(
                    "跳过偏移非法的行：char_start=%s > char_end=%s",
                    line.char_start,
                    line.char_end,
                )
                continue
            valid.append(line)
        # 排序副本：**不修改调用方传入的列表**（调用方可能复用）
        ordered = sorted(valid, key=lambda item: (item.char_start, item.char_end))
        self._lines = ordered
        self._starts = [line.char_start for line in ordered]
        self._total_chars = max((line.char_end for line in ordered), default=0)
        self._indexed = True
        self._unindexed_warned = False

    # ---------------- 查询 ----------------
    def locate(self, start: int, end: int) -> list[NormalizedBBox]:
        """把字符区间换算为 BBox 列表（按阅读顺序）。

        Args:
            start: 起始偏移（含）。
            end: 结束偏移（**不含**）。

        Returns:
            覆盖区间内所有行的 BBox（按阅读顺序、已去重）；无命中返回 `[]`。

        Raises:
            不抛异常（契约：前端高频调用，必须稳定）。
        """
        if not self._indexed or not self._lines:
            if not self._unindexed_warned:
                self._unindexed_warned = True
                _LOG.warning("偏移索引尚未建立，locate() 返回空结果")
            return []

        total = self._total_chars
        start = max(0, min(int(start), total))
        end = max(0, min(int(end), total))
        if start >= end:
            return []

        boxes: list[NormalizedBBox] = []
        seen: set[tuple[int, float, float, float, float]] = set()
        # 二分定位：第一个 char_start <= start 的行（-1 回收可能跨越 start 的那一行）
        cursor = max(bisect_right(self._starts, start) - 1, 0)
        for line in self._lines[cursor:]:
            if line.char_start >= end:
                break  # 索引有序 → 可安全早停
            if line.char_end <= start:
                continue
            key = (
                line.page,
                round(line.x, 1),
                round(line.y, 1),
                round(line.width, 1),
                round(line.height, 1),
            )
            if key in seen:
                continue
            seen.add(key)
            boxes.append(
                NormalizedBBox(
                    page=line.page,
                    x=round(line.x, 1),
                    y=round(line.y, 1),
                    width=round(line.width, 1),
                    height=round(line.height, 1),
                    coordinate_system="PDF_POINTS",
                )
            )

        # 打点用 **DEBUG**：`locate()` 是前端高频只读路径（悬停/滚动逐次调用），
        # INFO 级打点会把"查索引"变成"写日志"（实测 1000 次调用从 ms 级涨到秒级）。
        # 需要排查高亮问题时把 `CG_RAG_LOG_LEVEL` 调到 DEBUG 即可看到每次调用。
        log_event(
            _LOG,
            "ocr.align.locate",
            level="DEBUG",
            trace_id=ensure_trace_id(),
            span=end - start,
            hits=len(boxes),
        )
        return boxes

    @property
    def indexed(self) -> bool:
        """是否已建立索引（`index_text_offsets()` 调过即为 True，哪怕是空列表）。"""
        return self._indexed

    @property
    def total_chars(self) -> int:
        """已索引文本的总长度（`max(char_end)`）。"""
        return self._total_chars


def build_bbox_aligner() -> BBoxAligner:
    """工厂：返回本机实现。

    Returns:
        `BBoxAligner` 实现（`to_pdf_points()` 由基类提供，**不要重写**）。
    """
    return LocalBBoxAligner()


__all__ = ["LocalBBoxAligner", "build_bbox_aligner"]
