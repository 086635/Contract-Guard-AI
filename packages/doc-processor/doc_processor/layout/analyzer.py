"""版面分析器（T-C3 / `3-7`）：**规则优先**，扫描件才兜底。

设计要点（先读这三条，再改代码）：
1. **规则优先**：数字版 PDF 的字号 / 加粗 / 表格线**本来就在文件里**（`pdfplumber` 一次就能拿到），
   上模型只会更慢、更不可复现。
2. **不做行聚类**：行只来自 `page.extract_text_lines()`（与 `3-6` 同一原语）——
   自己写 y 聚类会产生"两种行边界"，块与行会对不上。
3. **坐标零换算**：`pdfplumber` 原生就是 PDF 用户空间（72dpi、左上原点），本模块**不做任何缩放**。
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cg_observability import ensure_trace_id, get_logger, log_event

from ..config import OcrSettings, get_ocr_settings
from ..errors import UnsupportedFormatError
from ..router import DocRouter, RouteKind
from . import BlockType, LayoutAnalyzer, LayoutBlock

_LOG = get_logger("doc_processor.layout.analyzer")

# ---- 规则阈值（**唯一处**：不是可调业务参数，故不进 OcrSettings）----
BODY_SIZE_FALLBACK = 10.5      # 拿不到字号时的默认正文字号（pt）
TITLE_SIZE_RATIO = 1.2         # 字号 ≥ 正文中位数 × 该值 → TITLE
TITLE_MAX_CHARS = 40           # TITLE 的最长字符数（长行一定是正文）
MERGE_GAP_RATIO = 1.8          # 纵向间距 ≤ 行高 × 该值 → 视为同一段
FOOTER_ZONE_RATIO = 0.667      # 页面高度的下 1/3（签名区判定）

_SIGNATURE_KEYWORDS = ("甲方", "乙方", "签字", "盖章", "签署")
_SIGNATURE_DATE_HINTS = ("日期", "年", "月", "日")
_SIGNATURE_MAX_CHARS = 20
_SEAL_KEYWORDS = ("印章", "（章）", "加盖")

# 单例类型：一律独立成块，不与相邻块合并
_SINGLETON_TYPES = frozenset({BlockType.TITLE, BlockType.SEAL, BlockType.SIGNATURE})

# A4（点）：兜底整页块的缺省尺寸
_A4_POINTS = (595.0, 842.0)


@dataclass(frozen=True, slots=True)
class StyledLine:
    """版面层的行载体（**局部类型，不进任何对外接口**）。

    前 6 个字段与 `TextLine` 同义（PDF_POINTS / 左上原点），后 3 个是版面专用。

    Attributes:
        page: 页码（1-based）。
        text: 行文本（保留原文，不 strip 内部空白）。
        x: 行框左上 x（PDF_POINTS）。
        y: 行框顶部 y（PDF_POINTS，左上原点）。
        width: 行框宽。
        height: 行框高。
        size: 行内字符的字号中位数。
        bold: 行内任一字符的字体名含 "Bold"。
        in_table: 行框与表格框相交。
    """

    page: int
    text: str
    x: float
    y: float
    width: float
    height: float
    size: float = BODY_SIZE_FALLBACK
    bold: bool = False
    in_table: bool = False


def _rect(line: StyledLine) -> tuple[float, float, float, float]:
    """行框 → `(x0, top, x1, bottom)`。"""
    return line.x, line.y, line.x + line.width, line.y + line.height


def _intersects(
    line: StyledLine, boxes: Sequence[tuple[float, float, float, float]]
) -> bool:
    """行框是否与任一框相交（AABB）。

    Args:
        line: 行。
        boxes: `(x0, top, x1, bottom)` 列表。

    Returns:
        相交返回 True。
    """
    x0, top, x1, bottom = _rect(line)
    for bx0, btop, bx1, bbottom in boxes:
        if x0 < bx1 and x1 > bx0 and top < bbottom and bottom > btop:
            return True
    return False


def _looks_like_signature(text: str) -> bool:
    """是否像签署栏文字：含签署关键词，且（带日期线索 或 行很短）。"""
    if not any(keyword in text for keyword in _SIGNATURE_KEYWORDS):
        return False
    if len(text) <= _SIGNATURE_MAX_CHARS:
        return True
    return any(hint in text for hint in _SIGNATURE_DATE_HINTS)


def classify_line(
    line: StyledLine,
    *,
    body_size: float,
    table_boxes: Sequence[tuple[float, float, float, float]] = (),
    page_height: float | None = None,
) -> BlockType:
    """判定单行的块类型（**规则表唯一处**，见 `3-7 §5.4`）。

    Args:
        line: 待判定行。
        body_size: 本页正文字号（非空行字号的中位数）。
        table_boxes: 本页表格框 `(x0, top, x1, bottom)`（`pdfplumber.find_tables()` 产出）。
        page_height: 本页高度；为 `None` 时**跳过**签名区规则（宁可不判，也不误判正文）。

    Returns:
        `BlockType`；空行返回 `UNKNOWN`（由 `merge_blocks()` 丢弃，**不产出空块**）。
    """
    text = line.text.strip()
    if not text:
        return BlockType.UNKNOWN
    if line.in_table or _intersects(line, table_boxes):
        return BlockType.TABLE
    if page_height is not None and line.y >= page_height * FOOTER_ZONE_RATIO:
        if _looks_like_signature(text):
            return BlockType.SIGNATURE
    if any(keyword in text for keyword in _SEAL_KEYWORDS):
        return BlockType.SEAL
    if (line.size >= body_size * TITLE_SIZE_RATIO or line.bold) and len(text) <= TITLE_MAX_CHARS:
        return BlockType.TITLE
    return BlockType.PARAGRAPH


def _body_size_of(lines: Sequence[StyledLine]) -> float:
    """本页正文字号 = 非空行字号的中位数；取不到时用 `BODY_SIZE_FALLBACK`。"""
    sizes = [float(line.size) for line in lines if line.text.strip()]
    if not sizes:
        return BODY_SIZE_FALLBACK
    return float(statistics.median(sizes))


def _build_block(rows: Sequence[StyledLine], kind: BlockType, page: int) -> LayoutBlock:
    """把若干行合成一个块（bbox = 并集；text = 行文本以 `"\n"` 拼接）。"""
    x0 = min(row.x for row in rows)
    y0 = min(row.y for row in rows)
    x1 = max(row.x + row.width for row in rows)
    y1 = max(row.y + row.height for row in rows)
    return LayoutBlock(
        block_type=kind,
        page=page,
        x=x0,
        y=y0,
        width=x1 - x0,
        height=y1 - y0,
        text="\n".join(row.text for row in rows),
    )


def merge_blocks(
    lines: Sequence[StyledLine],
    *,
    page: int,
    body_size: float | None = None,
    table_boxes: Sequence[tuple[float, float, float, float]] = (),
    page_height: float | None = None,
) -> list[LayoutBlock]:
    """把同页相邻同类行归并为块（**归并规则唯一处**，见 `3-7 §5.4`）。

    Args:
        lines: 同一页的行（按阅读顺序）。
        page: 页码（1-based）。
        body_size: 正文字号；为 `None` 时按本页非空行字号中位数推算。
        table_boxes: 表格框（透传给 `classify_line`）。
        page_height: 页高（透传给 `classify_line`；`None` 时跳过签名区规则）。

    Returns:
        `LayoutBlock` 列表（顺序 = 阅读顺序）。
    """
    resolved_body = _body_size_of(lines) if body_size is None else float(body_size)
    blocks: list[LayoutBlock] = []
    current: list[StyledLine] = []
    current_kind: BlockType | None = None

    def flush() -> None:
        """收尾当前块。"""
        nonlocal current, current_kind
        if current and current_kind is not None:
            blocks.append(_build_block(current, current_kind, page))
        current, current_kind = [], None

    for line in lines:
        kind = classify_line(
            line,
            body_size=resolved_body,
            table_boxes=table_boxes,
            page_height=page_height,
        )
        if kind is BlockType.UNKNOWN:  # 空行：天然段落边界，且不产出空块
            flush()
            continue
        previous = current[-1] if current else None
        gap = (line.y - (previous.y + previous.height)) if previous is not None else 0.0
        same_block = (
            previous is not None
            and kind is current_kind
            and kind not in _SINGLETON_TYPES
            and gap <= previous.height * MERGE_GAP_RATIO
        )
        if same_block:
            current.append(line)
            continue
        flush()
        current, current_kind = [line], kind
    flush()
    return blocks


class LocalLayoutAnalyzer(LayoutAnalyzer):
    """规则优先的版面分析实现。

    Attributes:
        settings: 配置。
    """

    def __init__(
        self,
        settings: OcrSettings | None = None,
        router: DocRouter | None = None,
    ) -> None:
        """初始化。

        Args:
            settings: 配置；为空取单例。
            router: 分流器；为空自建（用于区分数字版 PDF 与扫描件）。
        """
        self._settings: OcrSettings = settings or get_ocr_settings()
        self._router: DocRouter = router or DocRouter(self._settings)

    @property
    def settings(self) -> OcrSettings:
        """配置。"""
        return self._settings

    def analyze(self, pdf_path: str) -> list[LayoutBlock]:
        """对 PDF 做版面分析（契约方法）。

        Args:
            pdf_path: PDF 路径。

        Returns:
            `LayoutBlock` 列表（按阅读顺序，跨页顺序拼接）。

        Raises:
            UnsupportedFormatError: 文件不存在、扩展名不支持，或输入是 DOCX
                （DOCX 无坐标真值，其"版面"= 段落样式，属 `3-4` 的直抽路径）。
        """
        source = Path(pdf_path)
        if not source.is_file():
            raise UnsupportedFormatError(source, "文件不存在或不是普通文件")
        decision = self._router.route(source)

        if decision.kind is RouteKind.DOCX:
            raise UnsupportedFormatError(
                source, "DOCX 不做版面分析（其版面=段落样式，属 3-4 直抽路径）"
            )
        if decision.kind is not RouteKind.PDF_NATIVE:
            return self._fallback_blocks(source, decision)

        started = ensure_trace_id()
        blocks = self._analyze_native(source, trace_id=started)
        log_event(
            _LOG,
            "layout.analyze",
            trace_id=started,
            path=source.name,
            kind=decision.kind.value,
            blocks=len(blocks),
            titles=sum(1 for b in blocks if b.block_type is BlockType.TITLE),
            tables=sum(1 for b in blocks if b.block_type is BlockType.TABLE),
            seals=sum(1 for b in blocks if b.block_type is BlockType.SEAL),
        )
        return blocks

    def analyze_lines(
        self,
        lines: Sequence[StyledLine],
        *,
        page: int,
        page_height: float | None = None,
        table_boxes: Sequence[tuple[float, float, float, float]] = (),
    ) -> list[LayoutBlock]:
        """纯函数入口：由已构造好的行做单页版面分析（便于单测，不碰文件系统）。

        Args:
            lines: 本页行（按阅读顺序）。
            page: 页码（1-based）。
            page_height: 页高；`None` 时跳过签名区规则。
            table_boxes: 表格框。

        Returns:
            `LayoutBlock` 列表。
        """
        return merge_blocks(
            lines,
            page=page,
            body_size=_body_size_of(lines),
            table_boxes=table_boxes,
            page_height=page_height,
        )

    # ---------------- 内部：数字版 PDF（规则法） ----------------
    def _analyze_native(
        self, source: Path, *, trace_id: str
    ) -> list[LayoutBlock]:
        """数字版 PDF：逐页用 `pdfplumber` 取行 + 字号 + 表格框，走规则法。"""
        import pdfplumber

        blocks: list[LayoutBlock] = []
        try:
            with pdfplumber.open(source) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    # `find_tables()` 是页级重活：**每页只调一次**（3-7 §⑥-10）
                    table_boxes = [tuple(float(v) for v in t.bbox) for t in page.find_tables()]
                    styled = self._styled_lines(page, page_number, table_boxes, trace_id=trace_id)
                    blocks.extend(
                        self.analyze_lines(
                            styled,
                            page=page_number,
                            page_height=float(page.height),
                            table_boxes=table_boxes,
                        )
                    )
        except UnsupportedFormatError:
            raise
        except Exception as exc:  # 统一转领域异常
            raise UnsupportedFormatError(source, f"PDF 解析失败：{exc!r}") from exc
        return blocks

    def _styled_lines(
        self,
        page: Any,
        page_number: int,
        table_boxes: Sequence[tuple[float, float, float, float]],
        *,
        trace_id: str,
    ) -> list[StyledLine]:
        """由 `pdfplumber` 页对象构造带样式的行（**行切分只用库能力**）。"""
        styled: list[StyledLine] = []
        missing_size = 0
        for raw in page.extract_text_lines():
            chars = raw.get("chars") or []
            sizes = [
                float(char["size"]) for char in chars if char.get("size") is not None
            ]
            if not sizes:
                missing_size += 1
            size = statistics.median(sizes) if sizes else BODY_SIZE_FALLBACK
            bold = any("Bold" in str(char.get("fontname", "")) for char in chars)
            x0, x1 = float(raw["x0"]), float(raw["x1"])
            top, bottom = float(raw["top"]), float(raw["bottom"])
            line = StyledLine(
                page=page_number,
                text=str(raw.get("text") or ""),
                x=x0,
                y=top,
                width=x1 - x0,
                height=bottom - top,
                size=float(size),
                bold=bold,
            )
            styled.append(replace(line, in_table=_intersects(line, table_boxes)))
        if missing_size:
            # 不确定就不假装确定：记一条，便于排查"为什么这页全是 PARAGRAPH"
            log_event(
                _LOG,
                "layout.no_font_size",
                level="WARNING",
                trace_id=trace_id,
                page=page_number,
                lines=missing_size,
                note=f"已回落 BODY_SIZE_FALLBACK={BODY_SIZE_FALLBACK}",
            )
        return styled

    # ---------------- 内部：扫描件 / 图片兜底 ----------------
    def _fallback_blocks(
        self, source: Path, decision: Any
    ) -> list[LayoutBlock]:
        """扫描件 / 图片：无字号信息 → 逐页产出整页占位块（**不返回空**）。"""
        _LOG.warning(
            "版面分析退化为兜底（%s）：无字号信息，块类型统一为占位 PARAGRAPH",
            decision.kind.value,
        )
        sizes = self._page_sizes(source, decision)
        return [
            LayoutBlock(
                block_type=BlockType.PARAGRAPH,
                page=index,
                x=0.0,
                y=0.0,
                width=width,
                height=height,
                text="",
            )
            for index, (width, height) in enumerate(sizes, start=1)
        ]

    def _page_sizes(self, source: Path, decision: Any) -> list[tuple[float, float]]:
        """取占位块的页面尺寸（PDF 点）。

        扫描件 PDF：用 `pdfplumber` 的页宽高（**本就属于 PDF 用户空间，零换算**）；
        图片：**统一用 A4 占位** —— 本模块不做任何"像素 → 点"换算（否则会出现第二处缩放，
        `3-7 §⑥-3` 明令禁止；真实几何由 `3-6` 基于 OCR 行给出）。
        """
        if decision.kind is RouteKind.PDF_SCANNED:
            import pdfplumber

            try:
                with pdfplumber.open(source) as pdf:
                    sizes = [(float(p.width), float(p.height)) for p in pdf.pages]
            except Exception as exc:  # 统一转领域异常
                raise UnsupportedFormatError(source, f"PDF 解析失败：{exc!r}") from exc
            return sizes or [_A4_POINTS]

        return [_A4_POINTS]


def build_layout_analyzer(settings: OcrSettings | None = None) -> LayoutAnalyzer:
    """工厂：返回本机实现（与 `3-6` 的接线约定）。

    Args:
        settings: 配置；为空取单例。

    Returns:
        `LayoutAnalyzer` 实现。
    """
    return LocalLayoutAnalyzer(settings)


__all__ = [
    "BODY_SIZE_FALLBACK",
    "FOOTER_ZONE_RATIO",
    "MERGE_GAP_RATIO",
    "TITLE_MAX_CHARS",
    "TITLE_SIZE_RATIO",
    "LocalLayoutAnalyzer",
    "StyledLine",
    "build_layout_analyzer",
    "classify_line",
    "merge_blocks",
]
