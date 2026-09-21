"""要素与条款抽取（T-C4 / `3-4`）：**规则优先，绝不编造**。

三条纪律（先读再改）：
1. 🔴 **条款编号正则只有一处**：`rag_contracts.patterns.CLAUSE_HEAD_RE`（本文件 import 它）。
   另写一份会让「解析层认为的第 3 条 ≠ 检索层认为的第 3 条」，溯源与高亮整体错位。
2. 🔴 **不编造**：要素未命中 → `value=None` + `confidence=0.0`；占位符（"甲方：________"）同样置空。
3. 🔴 **不 import `aligner`**（`parser ↔ aligner` 循环导入）：本模块的坐标已是 PDF_POINTS，
   **不做任何换算**；`TextLine` 只定义与导出，单向被 `aligner` / `3-5` / `3-6` 复用。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cg_observability import ensure_trace_id, get_logger, log_event
from rag_contracts.patterns import CLAUSE_HEAD_RE

from ..config import OcrSettings, get_ocr_settings
from ..errors import UnsupportedFormatError
from ..router import DocRouter, RouteKind
from . import NodeExtractor, ParsedClause, ParsedElement

_LOG = get_logger("doc_processor.parser.extractor")

#: 甲方 / 乙方的抽取范围：只看前 N 页（合同要素集中在首部，避免正文里"甲方应……"被当成主体名）
PARTY_PAGES = 2

# 要素规则表（**唯一处**：表驱动，便于回归与扩展）
#   (key, label, pattern, base_confidence)
_ELEMENT_RULES: tuple[tuple[str, str, re.Pattern[str], float], ...] = (
    ("party_a", "甲方", re.compile(r"甲\s*方[：:\s]*(.{2,40})"), 0.90),
    ("party_b", "乙方", re.compile(r"乙\s*方[：:\s]*(.{2,40})"), 0.90),
    (
        "amount",
        "合同金额",
        re.compile(r"([¥￥]|人民币)?\s*([0-9][0-9,，]*(?:\.[0-9]{1,2})?)\s*(元|万元)"),
        0.85,
    ),
    (
        "sign_date",
        "签订日期",
        re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{2}-\d{2}"),
        0.90,
    ),
    (
        "term",
        "合同期限",
        re.compile(r"自.{2,40}?至.{2,40}|\d+\s*(?:个月|年)(?![0-9])"),
        0.70,
    ),
    ("jurisdiction", "管辖地", re.compile(r".{2,30}(人民法院|仲裁委员会)"), 0.75),
)

#: 每个要素取正则的哪一组作为值（**唯一处**）：缺省 0 = 整段命中。
#:   · 甲乙方：组 1（主体名，去掉标签）
#:   · 金额：组 2（数字部分，不含币种与单位 —— 见 §⑦ 断言 6 的期望值 `12,000.00`）
#:   · 日期 / 期限 / 管辖地：整段命中（管辖地保留"北京市朝阳区"这类前缀，故不取只含机构名的组 1）
_VALUE_GROUP: dict[str, int] = {
    "party_a": 1,
    "party_b": 1,
    "amount": 2,
    "sign_date": 0,
    "term": 0,
    "jurisdiction": 0,
}

#: 大写金额（与数字金额同时命中 → 置信度升到 0.95）
_AMOUNT_UPPER_RE = re.compile(r"[壹贰叁肆伍陆柒捌玖拾佰仟万亿]+元整")

#: 占位符：全是下划线 / 破折号 / 空格 / 句点 → 视为"未填写"
_PLACEHOLDER_RE = re.compile(r"^[_\-—\s\.。]+$")

#: 值截断符：同一行里出现下一个标签或连续空白时只取前半段
_VALUE_STOP_RE = re.compile(r"\s{2,}|甲方|乙方|签署|签订|日期|金额|期限|管辖|电话|地址")


@dataclass(frozen=True, slots=True)
class TextLine:
    """行级文本单元（解析层的通用"行"）。

    Attributes:
        page: 页码（1-based）。
        text: 行文本（保留原文）。
        x: 行框左上 x（PDF_POINTS）。
        y: 行框顶部 y（PDF_POINTS，左上原点）。
        width: 行框宽。
        height: 行框高。
        char_start: 在**全文字符串**中的起始偏移（0-based，半开区间）。
        char_end: 结束偏移（不含）。
    """

    page: int
    text: str
    x: float
    y: float
    width: float
    height: float
    char_start: int
    char_end: int


def _assemble(rows: Sequence[tuple[int, str, float, float, float, float]]) -> list[TextLine]:
    """把 `(页, 文本, x, y, w, h)` 行序列装配为 `TextLine`（补字符偏移）。

    ⚠️ 偏移口径与 `3-6 §⑤.1` 的 `assemble_text_lines()` **相同**（分隔符 `"\n"`、
    空行不占偏移）：本函数只服务 `extract_*_by_path()` 这类**单文件快捷入口**；
    **管道路径一律用 3-6 装配好的行**（那里才是唯一装配处）。

    Args:
        rows: 行元组序列（阅读顺序）。

    Returns:
        `TextLine` 列表（空行已剔除）。
    """
    lines: list[TextLine] = []
    cursor = 0
    for page, text, x, y, width, height in rows:
        if not text.strip():
            continue
        char_start = cursor
        char_end = char_start + len(text)
        lines.append(
            TextLine(
                page=int(page),
                text=text,
                x=float(x),
                y=float(y),
                width=float(width),
                height=float(height),
                char_start=char_start,
                char_end=char_end,
            )
        )
        cursor = char_end + 1  # +1 = 分隔符 "\n"
    return lines


class LocalNodeExtractor(NodeExtractor):
    """要素与条款抽取（规则优先）。

    Attributes:
        settings: 配置。
    """

    def __init__(self, settings: OcrSettings | None = None) -> None:
        """初始化。

        Args:
            settings: 配置；为空取单例。
        """
        self._settings: OcrSettings = settings or get_ocr_settings()
        self._router = DocRouter(self._settings)

    @property
    def settings(self) -> OcrSettings:
        """配置。"""
        return self._settings

    # ---------------- 对外：要素 ----------------
    def extract_elements(self, source_path: str) -> list[ParsedElement]:
        """抽取合同要素（甲乙方 / 金额 / 签订日期 / 期限 / 管辖地）。

        甲方 / 乙方只在前 `PARTY_PAGES` 页里找（合同要素集中在首部）。

        Args:
            source_path: 源文件路径。

        Returns:
            固定顺序的要素列表（未命中项 `value=None` / `confidence=0.0`）。
        """
        lines = self._lines_from_path(source_path)
        head_text = "\n".join(
            line.text for line in lines if line.page <= PARTY_PAGES
        )
        return self.elements_from_text(head_text)

    def elements_from_text(self, text: str) -> list[ParsedElement]:
        """纯函数版要素抽取（便于单测，不碰文件系统）。

        Args:
            text: 待抽取文本（调用方决定范围；甲乙方建议只传前 2 页）。

        Returns:
            与 `_ELEMENT_RULES` 同序的要素列表（未命中不编造）。
        """
        trace_id = ensure_trace_id()
        elements: list[ParsedElement] = []
        for key, label, pattern, base_confidence in _ELEMENT_RULES:
            match = pattern.search(text)
            if match is None:
                elements.append(
                    ParsedElement(key=key, label=label, value=None, confidence=0.0)
                )
                continue
            # 组号越界时回落到整段命中（正则可能没有捕获组，例如 sign_date）
            group_index = min(_VALUE_GROUP.get(key, 0), match.re.groups)
            value = self._clean_value(match.group(group_index))
            if value is None:
                log_event(
                    _LOG,
                    "ocr.extract.placeholder",
                    level="WARNING",
                    trace_id=trace_id,
                    key=key,
                    detail="命中占位符，按未填写处理（不编造）",
                )
                elements.append(
                    ParsedElement(key=key, label=label, value=None, confidence=0.0)
                )
                continue
            confidence = base_confidence
            if key == "amount" and _AMOUNT_UPPER_RE.search(text):
                confidence = 0.95  # 数字与大写金额互相印证
            elements.append(
                ParsedElement(
                    key=key, label=label, value=value, confidence=confidence
                )
            )
        log_event(
            _LOG,
            "ocr.extract",
            trace_id=trace_id,
            elements=len(elements),
            hit=sum(1 for item in elements if item.value is not None),
        )
        return elements

    @staticmethod
    def _clean_value(raw: str | None) -> str | None:
        """清洗捕获值：去首尾空白 / 截断到下一个标签 / 占位符与空值返回 None。"""
        if raw is None:
            return None
        value = raw.strip()
        if not value or _PLACEHOLDER_RE.match(value):
            return None
        stop = _VALUE_STOP_RE.search(value)
        if stop and stop.start() > 0:
            value = value[: stop.start()]
        value = value.strip(" \t:：,，;；。.、")
        if not value or _PLACEHOLDER_RE.match(value):
            return None
        return value

    # ---------------- 对外：条款 ----------------
    def extract_clauses(self, source_path: str) -> list[ParsedClause]:
        """抽取条款并构建 AST（level / parent_id / children_ids）。

        Args:
            source_path: 源文件路径。

        Returns:
            条款列表（文档顺序，含层级关系）。
        """
        return self.clauses_from_lines(self._lines_from_path(source_path))

    def clauses_from_lines(self, lines: Sequence[TextLine]) -> list[ParsedClause]:
        """由行序列切分条款（**3-6 的主调用入口**）。

        Args:
            lines: 行序列（`char_start` 必须单调不减，否则该行记 warning 并跳过）。

        Returns:
            条款列表（文档顺序 + AST 回填）。
        """
        clauses: list[ParsedClause] = []
        current_id: str | None = None
        current_label = ""
        current_level = 0
        current_text: list[str] = []
        seq = 0
        last_start = -1

        def flush() -> None:
            """收尾当前条款。"""
            nonlocal current_id, current_label, current_level, current_text
            if current_id is not None:
                clauses.append(
                    ParsedClause(
                        id=current_id,
                        parent_id=None,
                        label=current_label,
                        text="\n".join(current_text),
                        level=current_level,
                        children_ids=[],
                    )
                )
            current_id, current_label, current_level, current_text = None, "", 0, []

        for line in lines:
            if line.char_start < last_start:
                _LOG.warning(
                    "行序偏移非单调（char_start=%s < %s），已跳过该行",
                    line.char_start,
                    last_start,
                )
                continue
            last_start = line.char_start
            match = CLAUSE_HEAD_RE.match(line.text.strip())
            if match is None:
                if current_id is not None:
                    current_text.append(line.text)
                continue
            flush()
            seq += 1
            current_id = f"clause-{seq:04d}"
            current_label = match.group("num").strip()
            current_level = self._level_of(current_label)
            current_text = [line.text]
        flush()
        return self.build_ast(clauses)

    @staticmethod
    def _level_of(num: str) -> int:
        """条款编号 → 层级（**唯一处**，见 `3-4 §⑤.3`）。

        Args:
            num: 编号文本（如 `第一章` / `第三条` / `6.2` / `1)`）。

        Returns:
            层级：章=1 / 条=2 / 款项=3 / `6.2`=2 / `3.1.2`=3 / `1)`=3 / 无法判定=0。
        """
        if num.endswith("章"):
            return 1
        if num.endswith("条"):
            return 2
        if num.endswith(("款", "项", ")")):
            return 3
        if "." in num:
            return 1 + num.count(".")
        return 0

    def build_ast(self, clauses: list[ParsedClause]) -> list[ParsedClause]:
        """回填层级关系：`parent_id` / `children_ids` / `level`（单遍 + 层级栈）。

        Args:
            clauses: 文档顺序的条款列表（本方法会清空并重填 `children_ids`，幂等）。

        Returns:
            同一列表（原地回填）。
        """
        stack: list[ParsedClause] = []
        for clause in clauses:
            clause.children_ids = []
        for clause in clauses:
            while stack and stack[-1].level >= clause.level:
                stack.pop()
            if stack:
                clause.parent_id = stack[-1].id
                stack[-1].children_ids.append(clause.id)
            else:
                clause.parent_id = None
            stack.append(clause)
        return clauses

    # ---------------- 内部：从文件取行 ----------------
    def _lines_from_path(self, source_path: str) -> list[TextLine]:
        """按分流结果从文件取行（docx 直抽 / PDF 直抽 / OCR）。

        Args:
            source_path: 源文件路径。

        Returns:
            `TextLine` 列表。

        Raises:
            UnsupportedFormatError: 文件不可用或无法解析。
        """
        source = Path(source_path)
        if not source.is_file():
            raise UnsupportedFormatError(source, "文件不存在或不是普通文件")
        decision = self._router.route(source)

        if decision.kind is RouteKind.DOCX:
            return _assemble(self._docx_rows(source))
        if decision.kind is RouteKind.PDF_NATIVE:
            return _assemble(self._pdf_rows(source))
        return _assemble(self._ocr_rows(source, decision))

    @staticmethod
    def _docx_rows(source: Path) -> list[tuple[int, str, float, float, float, float]]:
        """DOCX → 行（无坐标真值：坐标全 0，由 3-6 的 DOCX 分支统一标记为合成坐标）。"""
        from docx import Document  # 延迟导入：只有 docx 路径才需要它

        try:
            document = Document(str(source))
        except Exception as exc:  # 统一转领域异常
            raise UnsupportedFormatError(source, f"DOCX 解析失败：{exc!r}") from exc
        rows: list[tuple[int, str, float, float, float, float]] = []
        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if text:
                rows.append((1, text, 0.0, 0.0, 0.0, 0.0))
        return rows

    @staticmethod
    def _pdf_rows(source: Path) -> list[tuple[int, str, float, float, float, float]]:
        """数字版 PDF → 行（`pdfplumber` 逐行，坐标本就是 PDF_POINTS）。"""
        import pdfplumber

        rows: list[tuple[int, str, float, float, float, float]] = []
        try:
            with pdfplumber.open(source) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    for raw in page.extract_text_lines():
                        x0, x1 = float(raw["x0"]), float(raw["x1"])
                        top, bottom = float(raw["top"]), float(raw["bottom"])
                        rows.append(
                            (
                                page_number,
                                str(raw.get("text") or ""),
                                x0,
                                top,
                                x1 - x0,
                                bottom - top,
                            )
                        )
        except Exception as exc:  # 统一转领域异常
            raise UnsupportedFormatError(source, f"PDF 解析失败：{exc!r}") from exc
        return rows

    def _ocr_rows(
        self, source: Path, decision: Any
    ) -> list[tuple[int, str, float, float, float, float]]:
        """扫描件 / 图片 → 行（走 3-3 的引擎，坐标已归一为 PDF_POINTS）。"""
        from ..ocr.engine import build_ocr_engine  # 延迟导入

        engine = build_ocr_engine(self._settings)
        results: list[Any] = []
        if decision.kind is RouteKind.IMAGE:
            results = engine.ocr_page(str(source))
        else:
            import pdfplumber

            with pdfplumber.open(source) as pdf:
                page_count = len(pdf.pages)
            for page_index in range(page_count):
                results.extend(engine.ocr_pdf_page(str(source), page_index))
        return [
            (item.page, item.text, item.x, item.y, item.width, item.height)
            for item in results
        ]


def build_node_extractor(settings: OcrSettings | None = None) -> NodeExtractor:
    """工厂：返回本机实现（与 `3-6` 的接线约定）。

    Args:
        settings: 配置；为空取单例。

    Returns:
        `NodeExtractor` 实现。
    """
    return LocalNodeExtractor(settings)


__all__ = [
    "PARTY_PAGES",
    "LocalNodeExtractor",
    "TextLine",
    "build_node_extractor",
]
