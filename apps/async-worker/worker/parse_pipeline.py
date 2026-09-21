"""解析流水线（T-C7 / `3-6`）：源文件 → 分流 → 文本 → 要素/条款 → 偏移索引 → 结果信封。

三条纪律（先读再改）：
1. 🔴 **全文拼接只能发生在 `assemble_text_lines()`**：四条支路都调它；任务体与
   `clause_payloads()` 禁止二次拼接字符串（否则偏移与 `3-5` 的索引必然错位）。
2. 🔴 **本模块不做任何坐标换算**：PDF 原生坐标已是 PDF_POINTS；OCR 坐标由 `3-3` 归一；
   DOCX 无真值（走合成坐标并显式标记）。
3. 🔴 **任何异常都收敛成 `FAILED` 信封**：不抛给 broker（否则前端只剩一个没有原因的 FAILED）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cg_observability import ensure_trace_id, get_logger, log_event
from doc_processor.aligner import build_bbox_aligner
from doc_processor.config import OcrSettings, get_ocr_settings
from doc_processor.errors import DocProcessorError
from doc_processor.parser import TextLine, build_node_extractor
from doc_processor.parser.extractor import PARTY_PAGES
from doc_processor.router import DocRouter, RouteKind
from rag_contracts.patterns import CLAUSE_HEAD_RE

_LOG = get_logger("worker.parse")

#: 扫描件逐页 OCR 的成本上限（防演示时被超长 PDF 拖死；超出则截断并降级）
MAX_OCR_PAGES = 50

#: 解析工件目录（离线可验证的唯一"持久化"手段；见 §⑥-12）
ARTIFACT_DIR = Path("data/parse")

#: DOCX 顺序推断坐标用的行高（pt）—— DOCX 没有真值坐标
DOCX_LINE_HEIGHT_PT = 14.0

#: 一行 = (页, 文本, x, y, width, height)；坐标单位统一 PDF_POINTS
Row = tuple[int, str, float, float, float, float]


class SourceUnavailableError(RuntimeError):
    """源文件三种途径都拿不到（显式本地路径不存在 / 本地路径不存在 / MinIO 下载失败）。

    Attributes:
        file_key: 传入的对象键或路径。
        reason: 一句中文原因。
    """

    def __init__(self, file_key: str, reason: str) -> None:
        """初始化。

        Args:
            file_key: MinIO 对象键或本地路径。
            reason: 中文原因。
        """
        self.file_key = file_key
        self.reason = reason
        super().__init__(f"源文件不可用：{file_key}（{reason}）")


def assemble_text_lines(rows: Sequence[Row]) -> tuple[list[TextLine], str]:
    """把「页 / 文本 / 坐标」行序列装配为 `TextLine` + 全文（**字符偏移唯一处**）。

    规则（唯一处，四条支路共用）：
      1. 只保留 `text.strip()` 非空的行（空行不占偏移，避免前端高亮掐到空白）；
      2. 行间分隔符固定 `"\n"`；`char_start` = 上一行 `char_end` + 1；
      3. `char_end = char_start + len(text)`，**半开区间**（与 3-5 的 `locate()` 一致）；
      4. 不排序、不去重（阅读顺序由各支路负责）。

    Args:
        rows: 行元组序列（按阅读顺序）。

    Returns:
        `(lines, full_text)`。
    """
    lines: list[TextLine] = []
    cursor = 0
    for page, text, x, y, width, height in rows:
        if not text.strip():
            continue
        lines.append(
            TextLine(
                page=int(page),
                text=text,
                x=float(x),
                y=float(y),
                width=float(width),
                height=float(height),
                char_start=cursor,
                char_end=cursor + len(text),
            )
        )
        cursor = lines[-1].char_end + 1  # +1 是分隔符 "\n"
    return lines, "\n".join(line.text for line in lines)


def _clause_spans(lines: Sequence[TextLine]) -> list[tuple[int, int, str]]:
    """复扫行序列，得到每条条款的行区间 `(起始下标, 结束下标(含), label)`。

    边界规则与 `3-4 §5.4` 一致：**本编号行起、下一个编号行前一行止**。
    本函数只做"定位"，层级/父子仍以 `3-4` 的 AST 结果为准（不重复实现规则）。
    """
    spans: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = CLAUSE_HEAD_RE.match(line.text.strip())
        if match is None:
            continue
        if spans:
            spans[-1] = (spans[-1][0], index - 1, spans[-1][2])
        spans.append((index, len(lines) - 1, match.group("num").strip()))
    return spans


def clause_payloads(
    clauses: Sequence[Any],
    lines: Sequence[TextLine],
    aligner: Any,
    *,
    warnings: list[str] | None = None,
) -> list[dict]:
    """条款 → 可被 `2-6 to_clause_inputs()` 直接消费的 dict（**接缝唯一处**）。

    Args:
        clauses: `ParsedClause` 列表（`3-4` 产出）。
        lines: 装配后的行（用于定位行区间）。
        aligner: 已建立索引的 `LocalBBoxAligner`。
        warnings: 告警收集列表（可为 None）。

    Returns:
        与 `clauses` 等长的 dict 列表（键名见 `3-6 §5.4`）。
    """
    sink = warnings if warnings is not None else []
    spans = _clause_spans(lines)
    mismatch = 0
    payloads: list[dict] = []
    for index, clause in enumerate(clauses):
        page_no: int | None = None
        bbox: dict | None = None
        span = spans[index] if index < len(spans) else None
        if span is None or span[2] != clause.label:
            mismatch += 1
        else:
            start_line, end_line = lines[span[0]], lines[span[1]]
            boxes = aligner.locate(start_line.char_start, end_line.char_end)
            if boxes:
                page_no = start_line.page
                bbox = _union_bbox(boxes, page_no)
        payloads.append(
            {
                "clause_id": clause.id,
                "label": clause.label,
                "text": clause.text,
                "level": clause.level,
                "parent_id": clause.parent_id,
                "page_no": page_no,
                "bbox": bbox,
                "children_ids": list(clause.children_ids),
            }
        )
    if mismatch:
        sink.append(f"{mismatch} 条条款无法与行区间对齐，其 page_no/bbox 已置空（不猜）")
    return payloads


def write_parse_artifact(outcome: ParseOutcome, directory: Path | None = None) -> Path | None:
    """把 `to_payload()` 写成 `data/parse/{contractId}.json`（**原子写**）。

    Args:
        outcome: 解析结果信封。
        directory: 输出目录；为空用 `ARTIFACT_DIR`。

    Returns:
        写入路径；失败（磁盘/权限）返回 None 并追加 warning —— **工件失败不影响任务状态**。
    """
    target_dir = Path(directory) if directory is not None else ARTIFACT_DIR
    target = target_dir / f"{outcome.contract_id}.json"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(outcome.to_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)  # 先写 .tmp 再 replace：避免半截文件
        return target
    except OSError as exc:
        outcome.warnings.append(f"解析工件写入失败（不影响结果）：{type(exc).__name__}")
        return None


def resolve_source(file_key: str, local_path: str | None = None) -> Path:
    """定位源文件（**三条途径，顺序固定**）。

    1. `local_path` 指定且存在 → 直接用（显式指定却不存在 → 报错，**不静默回落**）；
    2. `file_key` 本身是存在的本地路径 → 直接用；
    3. 否则经 `get_minio_client().fget_object(bucket, file_key, tmpdir)` 下载。

    Args:
        file_key: MinIO 对象键**或**本地路径。
        local_path: 显式本地路径（联调/测试用，优先级最高）。

    Returns:
        可读的本地文件路径。

    Raises:
        SourceUnavailableError: 三条途径都失败（**不猜桶名、不猜路径**）。
    """
    if local_path:
        explicit = Path(local_path)
        if explicit.is_file():
            return explicit
        raise SourceUnavailableError(file_key, f"显式指定的本地路径不存在：{local_path}")

    candidate = Path(file_key)
    if candidate.is_file():
        return candidate

    import tempfile

    bucket = os.getenv("CG_MINIO_BUCKET", "contracts")  # 与 api-gateway 的 minio_bucket 同源
    try:
        from worker.storage import get_minio_client

        client = get_minio_client()
        target = Path(tempfile.mkdtemp(prefix="parse-")) / (candidate.name or "object.bin")
        client.fget_object(bucket, file_key, str(target))
        log_event(
            _LOG, "parse.fetch.minio", trace_id=ensure_trace_id(), bucket=bucket, key=file_key
        )
        return target
    except Exception as exc:
        raise SourceUnavailableError(
            file_key, f"本地路径不存在且 MinIO 下载失败（bucket={bucket}）：{exc!r}"
        ) from exc


class _NullAligner:
    """空定位器：**合成坐标路径（DOCX）专用**。

    DOCX 没有坐标真值，其坐标只是"按段落顺序推断"的占位值；
    用它去算 bbox 会画出一个**看起来精确、其实毫无意义**的高亮框 ——
    按 `3-6 §⑨-6`（缺坐标就是 None）的纪律，这里直接不产出任何框。
    """

    def locate(self, start: int, end: int) -> list[Any]:
        """恒返回空（接口与 `LocalBBoxAligner.locate()` 同形）。"""
        return []


def _union_bbox(boxes: Sequence[Any], page: int) -> dict | None:
    """把多个 BBox 并成 openapi `BBox` 形状（**并集框唯一处**）。

    跨页时只取**指定页**的框（单个 BBox 无法跨页；前端按起始页画高亮）。

    Args:
        boxes: `NormalizedBBox` 列表。
        page: 目标页（1-based）。

    Returns:
        `{page, x, y, width, height, coordinateSystem}`；无该页的框时返回 None。
    """
    same_page = [box for box in boxes if box.page == page]
    if not same_page:
        return None
    x0 = min(box.x for box in same_page)
    y0 = min(box.y for box in same_page)
    x1 = max(box.x + box.width for box in same_page)
    y1 = max(box.y + box.height for box in same_page)
    return {
        "page": page,
        "x": round(x0, 1),
        "y": round(y0, 1),
        "width": round(x1 - x0, 1),
        "height": round(y1 - y0, 1),
        "coordinateSystem": "PDF_POINTS",
    }


@dataclass(slots=True)
class ParseOutcome:
    """解析流水线结果信封（**不落库**：本仓当前无 DB DAL）。

    Attributes:
        contract_id: 合同 ID。
        status: `SUCCESS` / `DEGRADED` / `FAILED`（判定表见 `3-6 §6.7`）。
        file_type: openapi `FileType`（取值与 `RouteKind` 逐字相同）。
        route_reason: 分流结论的可解释原因。
        page_count: 页数。
        elements / clauses / seal_detections / layout_blocks: 四类结构化产出。
        line_count / char_count: 行数与全文长度。
        coordinates_synthetic: DOCX 路径为 True（无坐标真值）。
        ocr_used / engine_name: 是否走了 OCR 与实际引擎名。
        warnings: 中文可读告警（**去重**）。
        stage_log: `{stage, status, latency_ms, detail}`（键名对齐 T-B2 的 `StageLog`）。
        artifact_path: 工件路径（写失败为 None）。
        took_ms: 端到端耗时。
        trace_id: 追踪 ID。
    """

    contract_id: str
    status: str = "FAILED"
    file_type: str = ""
    route_reason: str = ""
    page_count: int = 0
    elements: list[dict] = field(default_factory=list)
    clauses: list[dict] = field(default_factory=list)
    seal_detections: list[dict] = field(default_factory=list)
    layout_blocks: list[dict] = field(default_factory=list)
    line_count: int = 0
    char_count: int = 0
    coordinates_synthetic: bool = False
    ocr_used: bool = False
    engine_name: str = "none"
    warnings: list[str] = field(default_factory=list)
    stage_log: list[dict] = field(default_factory=list)
    artifact_path: str | None = None
    took_ms: int = 0
    trace_id: str = ""

    def to_payload(self) -> dict:
        """转 camelCase 信封（字段表见 `3-6 §5.3`）。"""
        return {
            "contractId": self.contract_id,
            "traceId": self.trace_id,
            "status": self.status,
            "fileType": self.file_type,
            "routeKind": self.file_type,
            "routeReason": self.route_reason,
            "pageCount": self.page_count,
            "lineCount": self.line_count,
            "charCount": self.char_count,
            "coordinatesSynthetic": self.coordinates_synthetic,
            "ocrUsed": self.ocr_used,
            "engineName": self.engine_name,
            "elements": self.elements,
            "clauses": self.clauses,
            "sealDetections": self.seal_detections,
            "layoutBlocks": self.layout_blocks,
            "warnings": self.warnings,
            "stageLog": self.stage_log,
            "artifactPath": self.artifact_path,
            "tookMs": self.took_ms,
        }


#: 只认前 `PARTY_PAGES` 页的要素（甲乙方）—— 其余要素可用全文兜底
_PARTY_KEYS = frozenset({"party_a", "party_b"})


class ParsePipeline:
    """解析流水线（**与 Celery 解耦**，可直接离线单测）。

    Attributes:
        settings: OCR / 解析配置。
    """

    def __init__(
        self,
        settings: OcrSettings | None = None,
        router: DocRouter | None = None,
        extractor: Any = None,
        ocr: Any = None,
        reporter: Any = None,
    ) -> None:
        """初始化（依赖可注入；`reporter is None` 时不报进度）。

        Args:
            settings: 配置；为空取单例。
            router: 分流器；为空自建。
            extractor: 抽取器；为空自建（`build_node_extractor`）。
            ocr: OCR 引擎；为空时在需要时惰性构造。
            reporter: 进度上报器（`ParseProgressReporter` 或桩对象）。
        """
        self._settings = settings or get_ocr_settings()
        self._router = router or DocRouter(self._settings)
        self._extractor = extractor or build_node_extractor(self._settings)
        self._ocr = ocr
        self._reporter = reporter
        self._seal_hooks: tuple[Any, Any] | None = None
        self._seal_loaded = False
        self._seal_warned = False
        self._layout_warned = False

    # ---------------- 主流程 ----------------
    def run(self, source_path: Path, contract_id: str) -> ParseOutcome:
        """跑完整条流水线（**任何异常都在此处收敛为 FAILED，不向外抛**）。

        Args:
            source_path: 已落到本地的源文件。
            contract_id: 合同 ID。

        Returns:
            `ParseOutcome`。
        """
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex
        outcome = ParseOutcome(contract_id=contract_id, trace_id=trace_id)
        degraded: list[str] = []
        sink = outcome.warnings
        # "只告警一次"的作用域是**一次 run**（防逐页刷屏），不是进程级 ——
        # 否则复用同一个 pipeline 跑第二份合同时会**静默丢掉降级**（实测踩到）。
        self._seal_warned = False
        self._layout_warned = False

        try:
            decision = self._router.route(source_path)
            outcome.file_type = decision.kind.value
            outcome.route_reason = decision.reason
            outcome.page_count = decision.page_count
            self._stage(outcome, "route", "OK", detail=decision.reason)
            self._report("route")
            log_event(
                _LOG,
                "parse.route",
                trace_id=trace_id,
                contract_id=contract_id,
                kind=decision.kind.value,
                reason=decision.reason,
            )

            # ① 文本（四路支路；扫描件支路在此顺带复用渲染图做印章检测）
            t0 = time.perf_counter()
            rows = self._collect_rows(source_path, decision, outcome, degraded, sink)
            lines, full_text = assemble_text_lines(rows)
            outcome.line_count, outcome.char_count = len(lines), len(full_text)
            if not lines:
                degraded.append("未抽取到任何文本行")
            self._stage(
                outcome,
                "parse_text",
                "OK" if lines else "DEGRADED",
                t0,
                f"{len(lines)} 行 / {len(full_text)} 字符",
            )
            self._report("parse_text")

            # ② 印章（剩余支路；已落盘的 3-8 才真正产出，否则只告警一次）
            self._collect_seals(source_path, decision, outcome, degraded, sink)
            self._stage(outcome, "seal", "OK", detail=f"{len(outcome.seal_detections)} 枚")
            self._report("seal")

            # ③ 版面（3-7，P1 软依赖：整文档一次调用，内部逐页）
            outcome.layout_blocks = self._collect_layout(source_path, decision, sink)

            # ④ 要素与条款
            t0 = time.perf_counter()
            clauses = self._extractor.clauses_from_lines(lines)
            head_lines = [line for line in lines if line.page <= PARTY_PAGES]
            if not clauses:
                degraded.append("未切出任何条款（可能没有编号行）")
            self._stage(outcome, "extract", "OK", t0, f"{len(clauses)} 条条款")
            self._report("extract")

            # ⑤ 偏移索引 + 接缝组装
            t0 = time.perf_counter()
            if outcome.coordinates_synthetic:
                # 合成坐标（DOCX）：不产出任何几何框（见 `_NullAligner` 的说明）
                aligner: Any = _NullAligner()
            else:
                aligner = build_bbox_aligner()
                aligner.index_text_offsets(lines)
            if head_lines and len(head_lines) != len(lines):
                _, head_text = assemble_text_lines(
                    [(line.page, line.text, line.x, line.y, line.width, line.height) for line in head_lines]
                )
            else:
                head_text = full_text
            outcome.elements = self._element_payloads(
                self._merge_elements(head_text, full_text), full_text, aligner
            )
            outcome.clauses = clause_payloads(clauses, lines, aligner, warnings=sink)
            self._stage(
                outcome,
                "index",
                "OK",
                t0,
                f"{len(outcome.elements)} 要素 / {len(outcome.clauses)} 条款",
            )
            self._report("index")

            outcome.status = "DEGRADED" if degraded else "SUCCESS"
        except DocProcessorError as exc:
            degraded.append(f"解析失败（{type(exc).__name__}）：{exc}")
            outcome.status = "FAILED"
            self._stage(outcome, "failed", "FAILED", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都转 FAILED 信封，绝不抛给 broker
            degraded.append(f"解析异常（{type(exc).__name__}）：{exc!r}")
            outcome.status = "FAILED"
            self._stage(outcome, "failed", "FAILED", detail=repr(exc))

        outcome.warnings.extend(degraded)
        artifact = write_parse_artifact(outcome)
        outcome.artifact_path = str(artifact) if artifact is not None else None
        outcome.warnings = _dedup(outcome.warnings)  # 去重：同类告警只留一条（防刷屏）
        outcome.took_ms = int((time.perf_counter() - started) * 1000)
        log_event(
            _LOG,
            "parse.done" if outcome.status != "FAILED" else "parse.fail",
            level="INFO" if outcome.status != "FAILED" else "WARNING",
            trace_id=trace_id,
            contract_id=contract_id,
            status=outcome.status,
            clauses=len(outcome.clauses),
            elements=len(outcome.elements),
            seals=len(outcome.seal_detections),
            took_ms=outcome.took_ms,
        )
        return outcome


    # ---------------- 分阶段实现 ----------------
    def _collect_rows(
        self,
        source: Path,
        decision: Any,
        outcome: ParseOutcome,
        degraded: list[str],
        sink: list[str],
    ) -> list[Row]:
        """按支路取行（四路动作表见 `3-6 §5.6`，**唯一映射处**）。"""
        kind = decision.kind
        if kind is RouteKind.DOCX:
            outcome.coordinates_synthetic = True
            degraded.append("DOCX 无坐标真值：已用顺序推断坐标，前端不得据此画精确高亮")
            return self._docx_rows(source)
        if kind is RouteKind.PDF_NATIVE:
            return self._pdf_rows(source)

        outcome.ocr_used = True
        engine = self._ocr_engine(degraded, sink)
        if engine is None:
            return []
        outcome.engine_name = getattr(engine, "engine_name", "none")
        if kind is RouteKind.PDF_SCANNED:
            return self._scanned_rows(source, engine, outcome, degraded, sink)
        # IMAGE：直接 OCR（按 image_dpi 解释坐标）
        rows: list[Row] = [
            (item.page, item.text, item.x, item.y, item.width, item.height)
            for item in engine.ocr_page(str(source))
        ]
        return rows

    @staticmethod
    def _docx_rows(source: Path) -> list[Row]:
        """DOCX → 行（**无坐标真值** → 顺序推断，并显式标记为合成坐标）。"""
        from docx import Document

        document = Document(str(source))
        rows: list[Row] = []
        for index, paragraph in enumerate(document.paragraphs):
            text = paragraph.text.strip()
            if not text:
                continue
            rows.append(
                (1, text, 0.0, index * DOCX_LINE_HEIGHT_PT, 0.0, DOCX_LINE_HEIGHT_PT)
            )
        return rows

    @staticmethod
    def _pdf_rows(source: Path) -> list[Row]:
        """数字版 PDF → 行（`extract_text_lines()`；坐标本就是 PDF_POINTS）。"""
        import pdfplumber

        rows: list[Row] = []
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
        return rows

    def _scanned_rows(
        self,
        source: Path,
        engine: Any,
        outcome: ParseOutcome,
        degraded: list[str],
        sink: list[str],
    ) -> list[Row]:
        """扫描件 → 行（渲染 DPI 由 `3-3` 归一；**印章复用同一张渲染图**）。"""
        import pdfplumber

        from doc_processor.ocr.engine import render_pdf_page

        with pdfplumber.open(source) as pdf:
            page_count = len(pdf.pages)
        pages = min(page_count, MAX_OCR_PAGES)
        if page_count > pages:
            degraded.append(
                f"扫描件页数 {page_count} 超过上限 {MAX_OCR_PAGES}，仅解析前 {pages} 页"
            )
        rows: list[Row] = []
        for page_index in range(pages):
            image, _, _ = render_pdf_page(
                str(source), page_index, dpi=self._settings.render_dpi
            )
            for item in engine.ocr_image(
                image, page=page_index + 1, src_dpi=self._settings.render_dpi
            ):
                rows.append(
                    (item.page, item.text, item.x, item.y, item.width, item.height)
                )
            self._collect_seals_image(
                image,
                page=page_index + 1,
                outcome=outcome,
                degraded=degraded,
            )
        return rows

    def _ocr_engine(self, degraded: list[str], sink: list[str]) -> Any:
        """取 OCR 引擎（惰性构造）；不可用时降级（**不抛给上层**）。"""
        if self._ocr is not None:
            return self._ocr
        from doc_processor.errors import OcrEngineUnavailableError
        from doc_processor.ocr.engine import build_ocr_engine

        try:
            self._ocr = build_ocr_engine(self._settings)
        except OcrEngineUnavailableError as exc:
            degraded.append(f"OCR 引擎不可用，文本为空：{exc}")
            return None
        return self._ocr


    # ---------------- 印章（3-8 软依赖） ----------------
    def _load_seal(self) -> tuple[Any, Any] | None:
        """取 `3-8`（`ocr/seal.py`）的两个入口；未落盘 → None（**只探测一次**）。"""
        if not self._seal_loaded:
            self._seal_loaded = True
            try:
                from doc_processor.ocr.seal import (
                    detect_seals_in_image,
                    to_detection_payload,
                )
            except ImportError:
                self._seal_hooks = None
            else:
                self._seal_hooks = (detect_seals_in_image, to_detection_payload)
        return self._seal_hooks

    def _collect_seals_image(
        self,
        image: Any,
        *,
        page: int,
        outcome: ParseOutcome,
        degraded: list[str],
    ) -> None:
        """对**内存图像**做印章检测（扫描件支路复用渲染图，不重复渲染）。

        ⚠️ 模块缺失时必须**同样走降级**（只告警一次）：否则扫描件支路会"静默 SUCCESS"，
        与"印章合规检查暂缺"的事实不符 —— 同一件事在不同支路上有两种状态，是最难查的一类不一致。
        """
        hooks = self._load_seal()
        if hooks is None:
            self._mark_seal_missing(degraded)
            return
        detect, to_payload = hooks
        analyses = detect(
            image,
            page=page,
            src_dpi=self._settings.render_dpi,
            settings=self._settings,
        )
        for analysis in analyses:
            outcome.seal_detections.append(
                to_payload(analysis, seq=len(outcome.seal_detections) + 1)
            )

    def _collect_seals(
        self,
        source: Path,
        decision: Any,
        outcome: ParseOutcome,
        degraded: list[str],
        sink: list[str],
    ) -> None:
        """印章检测（PDF_NATIVE / IMAGE 支路；扫描件已在取文本时完成）。"""
        kind = decision.kind
        if kind is RouteKind.DOCX or kind is RouteKind.PDF_SCANNED:
            return
        hooks = self._load_seal()
        if hooks is None:
            self._mark_seal_missing(degraded)
            return
        if kind is RouteKind.IMAGE:
            import cv2  # 读图为 BGR（与 3-3 的引擎入口同约定）

            image = cv2.imread(str(source), cv2.IMREAD_COLOR)
            if image is not None:
                self._collect_seals_image(
                    image,
                    page=1,
                    outcome=outcome,
                    degraded=degraded,
                )
            return

        from doc_processor.ocr.engine import render_pdf_page

        pages = min(decision.page_count, MAX_OCR_PAGES)
        for page_index in range(pages):
            image, _, _ = render_pdf_page(
                str(source), page_index, dpi=self._settings.render_dpi
            )
            self._collect_seals_image(
                image,
                page=page_index + 1,
                outcome=outcome,
                degraded=degraded,
            )

    def _mark_seal_missing(self, degraded: list[str]) -> None:
        """印章模块缺失：降级 + **只记一条** warning（不逐页刷屏）。"""
        if self._seal_warned:
            return
        self._seal_warned = True
        degraded.append("印章模块（3-8 / ocr/seal.py）未落盘，印章合规检查暂缺")

    # ---------------- 版面（3-7 软依赖） ----------------
    def _collect_layout(
        self, source: Path, decision: Any, sink: list[str]
    ) -> list[dict]:
        """版面块（**整文档一次调用**，`analyze()` 内部逐页；DOCX 跳过）。"""
        if decision.kind is RouteKind.DOCX:
            return []
        try:
            from doc_processor.layout.analyzer import build_layout_analyzer
        except ImportError:
            if not self._layout_warned:
                self._layout_warned = True
                sink.append("版面模块（3-7）未落盘，layoutBlocks 为空")
            return []
        try:
            blocks = build_layout_analyzer(self._settings).analyze(str(source))
        except DocProcessorError as exc:
            if not self._layout_warned:
                self._layout_warned = True
                sink.append(f"版面分析失败（已跳过）：{exc}")
            return []
        return [
            {
                "blockType": block.block_type.value,
                "page": block.page,
                "x": block.x,
                "y": block.y,
                "width": block.width,
                "height": block.height,
                "text": block.text,
            }
            for block in blocks
        ]

    # ---------------- 要素 ----------------
    def _merge_elements(self, head_text: str, full_text: str) -> list[Any]:
        """甲乙方只认**前 2 页**结果；其余要素用全文兜底（`3-6 §6.5` 口径）。"""
        head = self._extractor.elements_from_text(head_text)
        if head_text == full_text:
            return head
        full = self._extractor.elements_from_text(full_text)
        merged: list[Any] = []
        for head_item, full_item in zip(head, full):
            if head_item.key in _PARTY_KEYS or full_item.value is None:
                merged.append(head_item)
            else:
                merged.append(full_item)
        return merged

    @staticmethod
    def _element_payloads(elements: Sequence[Any], full_text: str, aligner: Any) -> list[dict]:
        """要素 → openapi `ContractElement` 形状（`bbox` 用全文首次出现定位）。"""
        payloads: list[dict] = []
        for item in elements:
            bbox = None
            if item.value and len(item.value) >= 2:
                index = full_text.find(item.value)
                if index >= 0:
                    boxes = aligner.locate(index, index + len(item.value))
                    if boxes:
                        bbox = _union_bbox(boxes, boxes[0].page)
            payloads.append(
                {
                    "key": item.key,
                    "label": item.label,
                    "value": item.value,
                    "confidence": item.confidence,
                    "bbox": bbox,
                    "isCustom": item.is_custom,
                }
            )
        return payloads

    # ---------------- 阶段记账与进度 ----------------
    @staticmethod
    def _stage(
        outcome: ParseOutcome,
        stage: str,
        status: str,
        started: float | None = None,
        detail: str = "",
    ) -> None:
        """写入 `stage_log`（键名对齐 T-B2 的 `StageLog`）。"""
        latency = 0
        if started is not None:
            latency = int((time.perf_counter() - started) * 1000)
        outcome.stage_log.append(
            {"stage": stage, "status": status, "latency_ms": latency, "detail": detail}
        )

    def _report(self, stage: str) -> None:
        """上报进度（`reporter is None` 时静默）。"""
        if self._reporter is not None:
            self._reporter.stage(stage)

    def report_fetch(self) -> None:
        """供任务体在取源文件后上报 `fetch` 阶段。"""
        self._report("fetch")

    def report_start(self) -> None:
        """供任务体在开工时上报开始。"""
        if self._reporter is not None:
            self._reporter.start()

    def report_finish(self, payload: dict) -> None:
        """供任务体在收尾时上报完成。"""
        if self._reporter is not None:
            self._reporter.finish(payload)

    def report_fail(self, reason: str) -> None:
        """供任务体在失败时上报（停在当前百分比）。"""
        if self._reporter is not None:
            self._reporter.fail(reason)

def _dedup(items: Sequence[str]) -> list[str]:
    """去重且保序（同类告警只留一条）。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def build_parse_pipeline(
    settings: OcrSettings | None = None, reporter: Any = None
) -> ParsePipeline:
    """工厂：返回本机实现（与 `3-6` 的接线约定）。

    Args:
        settings: 配置；为空取单例。
        reporter: 进度上报器。

    Returns:
        `ParsePipeline`。
    """
    return ParsePipeline(settings=settings, reporter=reporter)


__all__ = [
    "ARTIFACT_DIR",
    "MAX_OCR_PAGES",
    "ParseOutcome",
    "ParsePipeline",
    "Row",
    "SourceUnavailableError",
    "assemble_text_lines",
    "build_parse_pipeline",
    "clause_payloads",
    "resolve_source",
    "write_parse_artifact",
]
