"""文档分流器（T-C1 / `3-2`）。

🔴 **本模块最重要的一条红线**：**绝不对所有 PDF 都跑 OCR**。
数字版 PDF 用 `pdfplumber` 直抽**更准、更快、坐标天然精确**（PDF 用户空间 = 目标坐标系）；
OCR 只该用于"抽样页几乎抽不出字符"的扫描件与图片。很多团队在这里犯错，
对所有 PDF 都跑 OCR，白白引入识别误差。

判定依据是**抽样页的平均可抽字符数**，阈值来自 `OcrSettings.native_text_min_chars`
（**配置必须被消费**，禁止硬编码 50）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cg_observability import ensure_trace_id, get_logger, log_event

from .config import OcrSettings, get_ocr_settings
from .errors import UnsupportedFormatError

_LOG = get_logger("doc_processor.router")

_DOCX_SUFFIXES = frozenset({".docx"})
_PDF_SUFFIXES = frozenset({".pdf"})
_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
)


class RouteKind(str, Enum):  # noqa: UP042 - 与冻结契约 BlockType 保持同形（3-2 §⑤ 的接口规格）
    """输入文档的处理支路（模块 3 内部枚举，**不进契约层**）。"""

    DOCX = "DOCX"                # python-docx 直抽（结构化程度最高）
    PDF_NATIVE = "PDF_NATIVE"    # pdfplumber 直抽（逐词坐标，精度最高）
    PDF_SCANNED = "PDF_SCANNED"  # pypdfium2 渲染 → OCR
    IMAGE = "IMAGE"              # 直接 OCR


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """一次分流的结论（**必须可解释**）。

    Attributes:
        kind: 支路。
        reason: 中文可解释原因，**必须含关键数字**（便于回答"为什么这份走了 OCR"）。
        page_count: 页数；图片与 DOCX 记 1（DOCX 无页概念）。
        sampled_pages: 参与采样的页码（1-based，升序去重）；图片与 DOCX 为 `[1]`。
        avg_chars_per_page: 采样页的平均可抽字符数；图片与 DOCX 为 0.0。
    """

    kind: RouteKind
    reason: str
    page_count: int
    sampled_pages: list[int]
    avg_chars_per_page: float


class DocRouter:
    """文档分流器（**无状态**、可重复调用；不持有任何引擎句柄）。

    Attributes:
        settings: 配置（阈值与采样页数从这里取）。
    """

    def __init__(self, settings: OcrSettings | None = None) -> None:
        """初始化。

        Args:
            settings: 配置；为空则用 `get_ocr_settings()` 单例。
        """
        self._settings: OcrSettings = settings or get_ocr_settings()

    def route(self, path: str | Path) -> RouteDecision:
        """判定输入文件的处理支路。

        Args:
            path: 输入文件路径（`.docx` / `.pdf` / 图片）。

        Returns:
            分流结论。

        Raises:
            UnsupportedFormatError: 扩展名不在支持集合内，或文件不存在 / 不可读 / 已损坏。
        """
        source = Path(path)
        suffix = source.suffix.lower()
        if not source.is_file():
            raise UnsupportedFormatError(source, "文件不存在或不是普通文件")

        if suffix in _DOCX_SUFFIXES:
            decision = RouteDecision(
                kind=RouteKind.DOCX,
                reason=f"扩展名 {suffix} → DOCX 直抽（1 个文档，无需采样）",
                page_count=1,
                sampled_pages=[1],
                avg_chars_per_page=0.0,
            )
        elif suffix in _IMAGE_SUFFIXES:
            decision = RouteDecision(
                kind=RouteKind.IMAGE,
                reason=f"扩展名 {suffix} → 直接 OCR（1 页图片，无需采样）",
                page_count=1,
                sampled_pages=[1],
                avg_chars_per_page=0.0,
            )
        elif suffix in _PDF_SUFFIXES:
            decision = self._route_pdf(source)
        else:
            raise UnsupportedFormatError(
                source, f"扩展名 {suffix or '（无扩展名）'} 不在支持集合内"
            )

        log_event(
            _LOG,
            "ocr.route",
            trace_id=ensure_trace_id(),
            kind=decision.kind.value,
            page_count=decision.page_count,
            sampled_pages=decision.sampled_pages,
            avg_chars=round(decision.avg_chars_per_page, 1),
            threshold=self._settings.native_text_min_chars,
            reason=decision.reason,
        )
        return decision

    # ---------------- 内部 ----------------
    def _route_pdf(self, source: Path) -> RouteDecision:
        """PDF 支路：抽样测字符数 → 数字版直抽 / 扫描件走 OCR。"""
        page_count, sampled, avg = self._measure_pdf(source)
        threshold = self._settings.native_text_min_chars
        if avg >= threshold:
            return RouteDecision(
                kind=RouteKind.PDF_NATIVE,
                reason=(
                    f"抽样 {len(sampled)} 页平均 {avg:.1f} 字符 ≥ 阈值 {threshold}"
                    f" → 数字版直抽（共 {page_count} 页）"
                ),
                page_count=page_count,
                sampled_pages=sampled,
                avg_chars_per_page=avg,
            )
        return RouteDecision(
            kind=RouteKind.PDF_SCANNED,
            reason=(
                f"抽样 {len(sampled)} 页平均 {avg:.1f} 字符 < 阈值 {threshold}"
                f" → 渲染后 OCR（共 {page_count} 页）"
            ),
            page_count=page_count,
            sampled_pages=sampled,
            avg_chars_per_page=avg,
        )

    def _measure_pdf(self, source: Path) -> tuple[int, list[int], float]:
        """抽样测量 PDF 的可抽字符数。

        Args:
            source: PDF 路径。

        Returns:
            `(总页数, 采样页码, 采样页平均可抽字符数)`。

        Raises:
            UnsupportedFormatError: PDF 无法解析（损坏 / 加密 / 非 PDF 内容）。
        """
        # 延迟导入：分流本身很轻，没必要在 `import doc_processor` 时拖入 pdfminer 全家桶
        import pdfplumber

        try:
            with pdfplumber.open(source) as pdf:
                page_count = len(pdf.pages)
                sampled = self._sample_pages(page_count, self._settings.route_sample_pages)
                total = 0
                for page_no in sampled:
                    text = pdf.pages[page_no - 1].extract_text() or ""
                    total += len(text.strip())
        except Exception as exc:  # 统一转成可判定的领域异常（异常链已保留）
            raise UnsupportedFormatError(source, f"PDF 解析失败：{exc!r}") from exc
        avg = (total / len(sampled)) if sampled else 0.0
        return page_count, sampled, avg

    @staticmethod
    def _sample_pages(page_count: int, sample_size: int) -> list[int]:
        """采样页码（1-based，升序去重）。

        规则（`3-2 §⑥-1`，唯一处）：
          · 页数 ≤ 采样上限 → 全部页；
          · 否则取 **首 / 中 / 末**（`1`、`ceil(n/2)`、`n`），去重后可能少于 3 页。

        Args:
            page_count: 总页数。
            sample_size: 采样上限。

        Returns:
            升序去重的页码列表；`page_count == 0` 时返回 `[]`。
        """
        if page_count <= 0:
            return []
        if page_count <= sample_size:
            return list(range(1, page_count + 1))
        return sorted({1, math.ceil(page_count / 2), page_count})


__all__ = ["DocRouter", "RouteDecision", "RouteKind"]
