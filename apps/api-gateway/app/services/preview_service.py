"""C58 预览服务：状态查询 / 短期 URL 签发 / 重试派发。

设计要点：
1. `previewUrl` **运行时签发**（`MinioObjectStorage.presign_get`），永不落库；
   库里只有内部对象 key，且绝不进 API 响应。
2. 显式状态机：PDF 上传即 `DONE`（原文即可预览）；DOCX 初始 `PENDING`，
   由 `convert_doc`（C59）转换后置 `DONE`；失败置 `FAILED` 且可重试。
3. 重试**幂等**：`contract_previews.convert_task_id` 记录在飞转换任务，
   `RUNNING` 且已有任务时不再重复派发。
4. 权限与存在性：合同不可见/不存在都由路由统一映射 404，不泄露存在性。

对应：`文件集合/codex/4.C58预览元数据与预览API.md`。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.db.repository.auth import UserRecord
from app.db.repository.contract import (
    ContractDetail,
    ContractRepository,
    TaskRepository,
    owner_scope,
)
from app.db.repository.preview import PreviewRecord, PreviewRepository
from app.db.repository.result import ElementRepository
from app.generated.schemas import PreviewData, PreviewStatus, Status, TaskProgress
from app.services.commit_service import ContractNotFoundError
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from app.services.ingestion import IngestionService
from cg_observability import get_logger, log_event
from doc_processor.aligner import BBoxAligner, build_bbox_aligner
from doc_processor.parser import TextLine
from rag_contracts.chunk import BBOX_COORDINATE_SYSTEM, BBox, ContractChunk

#: 预览 URL 有效期（秒）：短期签名，前端到期后重新 GET。
PREVIEW_URL_TTL_SECONDS = 900

_PDF_SUFFIXES = frozenset({".pdf"})
_DOCX_SUFFIXES = frozenset({".docx"})
#: T-D8-2：图片原文即预览底图（与 PDF 同款"直接用原文"路径）。
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})

#: 预览来源类型（唯一处）：`PDF` / `IMAGE` 直接用原文，`DOCX` 需经 `convert` 队列。
SOURCE_PDF = "PDF"
SOURCE_DOCX = "DOCX"
SOURCE_IMAGE = "IMAGE"


_LOG = get_logger("api_gateway.services.preview")

#: 对齐时的最短可匹配字符数：太短的文本（如「甲方」）容易误配到其他位置。
MIN_ALIGN_CHARS = 8


@dataclass(frozen=True, slots=True)
class AlignmentReport:
    """坐标重建报告（C60 §⑤）：aligned / unaligned / mismatch 计数 + 安全摘要。"""

    contract_id: str
    aligned: int = 0
    unaligned: int = 0
    mismatch: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        """处理的切片总数。"""
        return self.aligned + self.unaligned + self.mismatch


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """折叠空白为单空格，并记录每个归一化字符对应的**原始**下标。

    比对必须在归一化空间做（DOCX 与 PDF 的换行/缩进不同），但定位必须回到原始
    偏移才能用 `BBoxAligner.locate()`。
    """
    chars: list[str] = []
    mapping: list[int] = []
    pending_space = False
    for index, char in enumerate(text):
        if char.isspace():
            pending_space = bool(chars)
            continue
        if pending_space:
            chars.append(" ")
            mapping.append(index)
            pending_space = False
        chars.append(char)
        mapping.append(index)
    return "".join(chars), mapping


def find_text_span(
    clause_text: str,
    *,
    normalized: str,
    mapping: Sequence[int],
) -> tuple[int, int] | None:
    """在归一化文本里定位条款文本，返回**原始** `[start, end)`；找不到返回 None。"""
    needle, _ = _normalize_with_map(clause_text)
    if len(needle) < MIN_ALIGN_CHARS:
        return None
    position = normalized.find(needle)
    if position < 0:
        return None
    return mapping[position], mapping[position + len(needle) - 1] + 1


def clamp_bbox(bbox: BBox, page_size: tuple[float, float] | None) -> BBox:
    """把框夹取到页面矩形内（越界 BBox 夹取）；页尺寸未知时只保证非负宽高。"""
    x0 = max(0.0, bbox.x)
    y0 = max(0.0, bbox.y)
    x1 = bbox.x + bbox.width
    y1 = bbox.y + bbox.height
    if page_size is not None:
        page_width, page_height = page_size
        if page_width > 0:
            x1 = min(x1, page_width)
        if page_height > 0:
            y1 = min(y1, page_height)
    return bbox.model_copy(
        update={
            "x": x0,
            "y": y0,
            "width": max(0.0, x1 - x0),
            "height": max(0.0, y1 - y0),
        }
    )


def union_bbox(
    boxes: Sequence[object],
    page: int | None,
    page_size: tuple[float, float] | None = None,
) -> BBox | None:
    """把同一页的多个行框并成条款框（**并集框唯一处**）；退化框返回 None。

    跨页时只取起始页（单个 BBox 无法跨页），与 `3-6 _union_bbox` 同口径。
    """
    if page is None:
        return None
    same_page = [box for box in boxes if getattr(box, "page", None) == page]
    if not same_page:
        return None
    x0 = min(float(getattr(box, "x")) for box in same_page)
    y0 = min(float(getattr(box, "y")) for box in same_page)
    x1 = max(
        float(getattr(box, "x")) + float(getattr(box, "width")) for box in same_page
    )
    y1 = max(
        float(getattr(box, "y")) + float(getattr(box, "height")) for box in same_page
    )
    union = clamp_bbox(
        BBox(
            page=int(page),
            x=x0,
            y=y0,
            width=max(0.0, x1 - x0),
            height=max(0.0, y1 - y0),
            coordinate_system=BBOX_COORDINATE_SYSTEM,
        ),
        page_size,
    )
    if union.width <= 0 or union.height <= 0:
        return None
    return union


def _page_of_span(span: tuple[int, int], lines: Sequence[TextLine]) -> int | None:
    """定位 span 起始字符所在行 → 页码（1-based）。"""
    start = span[0]
    for line in lines:
        if line.char_start <= start < line.char_end:
            return int(line.page)
    return int(lines[0].page) if lines else None


def align_chunks_to_lines(
    chunks: Sequence[ContractChunk],
    *,
    lines: Sequence[TextLine],
    aligner: BBoxAligner,
    page_sizes: Mapping[int, tuple[float, float]] | None = None,
    warnings: list[str] | None = None,
) -> tuple[list[ContractChunk], int, int, int]:
    """把条款文本与预览 PDF 行文本对齐，返回 `(更新后的切片, aligned, unaligned, mismatch)`。

    - 只改 `bbox` / `page_no`，**其余字段与 `chunk_id` 原样保留**（`bulk_upsert` 幂等的基础）；
    - 对齐失败保持 `bbox=None` 并写 warning，绝不伪造高亮（§② 不做 / ⑥-2）。
    """
    sink = warnings if warnings is not None else []
    haystack = "\n".join(line.text for line in lines)
    normalized, mapping = _normalize_with_map(haystack)

    updated: list[ContractChunk] = []
    aligned = unaligned = mismatch = 0
    for chunk in chunks:
        span = find_text_span(chunk.text, normalized=normalized, mapping=mapping)
        if span is None:
            unaligned += 1
            sink.append(f"条款 {chunk.section_id} 未在预览 PDF 中对齐，bbox 置空")
            updated.append(_without_bbox(chunk))
            continue
        page = _page_of_span(span, lines)
        box = union_bbox(
            aligner.locate(span[0], span[1]),
            page,
            (page_sizes or {}).get(page) if page is not None else None,
        )
        if box is None:
            mismatch += 1
            sink.append(f"条款 {chunk.section_id} 命中文本但缺少可用几何框，bbox 置空")
            updated.append(_without_bbox(chunk))
            continue
        aligned += 1
        updated.append(chunk.model_copy(update={"bbox": box, "page_no": box.page}))
    return updated, aligned, unaligned, mismatch


def _without_bbox(chunk: ContractChunk) -> ContractChunk:
    """把切片坐标清空（已有真值不动 → 避免把真实 PDF 的框清掉）。"""
    if chunk.bbox is None and chunk.page_no is None:
        return chunk
    return chunk.model_copy(update={"bbox": None, "page_no": None})


def extract_preview_lines(
    pdf_path: Path,
) -> tuple[list[TextLine], dict[int, tuple[float, float]]]:
    """从预览 PDF 抽行（原生坐标已是 PDF_POINTS）+ 每页尺寸（用于越界夹取）。

    字符偏移口径复用 `doc_processor` 的唯一装配口径（与 `3-6 §⑤.1` 相同）。
    """
    import pdfplumber

    from doc_processor.parser.extractor import _assemble

    rows: list[tuple[int, str, float, float, float, float]] = []
    page_sizes: dict[int, tuple[float, float]] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            page_sizes[page_number] = (float(page.width), float(page.height))
            for raw in page.extract_text_lines():
                text = str(raw.get("text") or "")
                if not text.strip():
                    continue
                x0, x1 = float(raw["x0"]), float(raw["x1"])
                top, bottom = float(raw["top"]), float(raw["bottom"])
                rows.append((page_number, text, x0, top, x1 - x0, bottom - top))
    return _assemble(rows), page_sizes


def _failure_reason(result: dict) -> str:
    """从转换结果里取第一条安全摘要（没有则给通用码）。"""
    warnings = result.get("warnings")
    if isinstance(warnings, list) and warnings:
        return str(warnings[0])
    return "CONVERT_FAILED"


def source_kind_of(filename: str) -> str | None:
    """按存储文件名判定预览来源类型。

    注意：这是**选择预览管线**用的分类，不是上传安全判据——
    真实内容类型已由 C56/C57 在服务端校验（magic sniff + 结构校验）。
    """
    suffix = Path(filename).suffix.lower()
    if suffix in _PDF_SUFFIXES:
        return SOURCE_PDF
    if suffix in _DOCX_SUFFIXES:
        return SOURCE_DOCX
    if suffix in _IMAGE_SUFFIXES:
        return SOURCE_IMAGE
    return None


def initial_preview_status(filename: str) -> PreviewStatus:
    """返回该来源类型的**初始**预览状态（PDF / 图片立即 DONE，DOCX 待转换）。"""
    kind = source_kind_of(filename)
    if kind in (SOURCE_PDF, SOURCE_IMAGE):
        return PreviewStatus.done
    if kind == SOURCE_DOCX:
        return PreviewStatus.pending
    return PreviewStatus.failed


def preview_object_key_for(contract: ContractDetail) -> str | None:
    """PDF / 图片的预览对象就是原文对象；DOCX 等待 C59 写入派生对象。"""
    if source_kind_of(contract.filename) in (SOURCE_PDF, SOURCE_IMAGE):
        return contract.storage_path
    return None


class PreviewService:
    """合同预览元数据服务。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        previews: PreviewRepository | None = None,
        contracts: ContractRepository | None = None,
        elements: ElementRepository | None = None,
        ingestion: IngestionService | None = None,
        tasks: TaskRepository | None = None,
    ) -> None:
        """构造服务。

        协作者可注入替身（与 `AuthService` 同款），使状态机、幂等与坐标重建
        **离线可测**；生产路径默认用当前 session 的真实仓储/服务。
        """
        self._context = context
        self._contracts = contracts or ContractRepository(context.session)
        self._previews = previews or PreviewRepository(context.session)
        self._elements = elements or ElementRepository(context.session)
        self._ingestion = ingestion or IngestionService(context)
        self._tasks = tasks or TaskRepository(context.session)

    async def get(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> PreviewData:
        """读取预览元数据；缺失时按来源类型初始化一行（幂等）。"""
        contract = await self._require_contract(contract_id, user)
        record = await self._previews.get_by_contract(contract_id)
        if record is None:
            record = await self._initialize(contract)
            await self._context.session.commit()
        elif record.status is PreviewStatus.running:
            reconciled = await self.reconcile_convert(contract_id)
            if reconciled is not None:
                record = reconciled
            await self._context.session.commit()
        return self._to_preview_data(record)

    async def retry(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> TaskProgress:
        """重试预览转换派发（幂等：在飞任务不重复派发）。"""
        contract = await self._require_contract(contract_id, user)
        record = await self._previews.get_by_contract(contract_id)
        if record is None:
            record = await self._initialize(contract)

        if record.status is PreviewStatus.done:
            await self._context.session.commit()
            return self._to_progress(record, Status.done, stage="done", percent=1.0)
        if record.status is PreviewStatus.running and record.convert_task_id:
            await self._context.session.commit()
            return self._to_progress(
                record, Status.running, stage="converting", percent=0.0
            )

        # 参数顺序即 C59 §⑤ 的跨进程契约：`convert_doc(contract_id, source_key)`。
        # C75-C①：**派发前**先落可恢复任务行（与 `ContractService.upload` 同一模式），
        # 否则转换任务只活在预览行里：stale 对账扫不到、失败无 row 可收敛。
        task = await self._tasks.create(
            contract_id,
            TaskKind.CONVERT_DOC.name,
            None,
        )
        try:
            celery_task_id = self._context.task_dispatcher.send(
                TaskKind.CONVERT_DOC,
                args=(str(contract_id), contract.storage_path),
            )
        except Exception as exc:
            await self._tasks.mark_failed(
                task.id,
                error_code="DISPATCH_FAILED",
                error_message=type(exc).__name__,
            )
            await self._context.session.commit()
            raise
        await self._tasks.attach_celery_task_id(task.id, celery_task_id)
        record = await self._previews.mark_running(record.id, celery_task_id)
        await self._context.session.commit()
        return self._to_progress(
            record, Status.running, stage="converting", percent=0.0
        )

    async def reconcile_convert(self, contract_id: UUID) -> PreviewRecord | None:
        """按 Celery 转换结果回写预览状态（**网关**职责；worker 不写业务 DB）。

        - 仍 `RUNNING` → 原样返回（下一次读取或 C63 reconciler 再试）；
        - Celery `DONE` 且结果 `status=SUCCESS` → 写派生 key/版本，置 `DONE`；
        - Celery `FAILED` 或结果 `status=FAILED` → 置 `FAILED`（附安全摘要）。

        Returns:
            回写后的记录；没有预览行/没有在飞任务/状态非 `RUNNING` 时返回当前值。
        """
        record = await self._previews.get_by_contract(contract_id)
        if record is None or not record.convert_task_id:
            return record
        if record.status is not PreviewStatus.running:
            return record

        snapshot = self._context.task_dispatcher.get_status(record.convert_task_id)
        if snapshot is None:
            return record
        result = snapshot.result if isinstance(snapshot.result, dict) else {}
        if snapshot.status == "FAILED" or result.get("status") == "FAILED":
            return await self._previews.mark_failed(
                record.id,
                error_message=_failure_reason(result),
            )
        if snapshot.status == "DONE" and result.get("status") == "SUCCESS":
            object_key = result.get("storagePath")
            if not isinstance(object_key, str) or not object_key:
                return await self._previews.mark_failed(
                    record.id,
                    error_message="CONVERT_NO_OUTPUT_KEY",
                )
            version = result.get("converterVersion")
            done = await self._previews.mark_done(
                record.id,
                preview_object_key=object_key,
                converter_version=str(version) if version else None,
            )
            # C60：转换完成后按**转换 PDF** 重建条款坐标（失败只告警，不阻断预览可用）。
            try:
                await self.rebuild_preview_bbox(contract_id)
            except Exception as exc:  # noqa: BLE001 - 坐标重建失败不得影响预览状态
                log_event(
                    _LOG,
                    "preview.align_failed",
                    level="WARNING",
                    contract_id=str(contract_id),
                    error=type(exc).__name__,
                )
            return done
        return record

    async def rebuild_preview_bbox(
        self,
        contract_id: UUID,
        preview_pdf: Path | None = None,
    ) -> AlignmentReport:
        """按**转换后 PDF** 重建条款坐标（C60 §⑤）。

        Args:
            contract_id: 合同 ID。
            preview_pdf: 已落盘的预览 PDF；为 `None` 时从对象存储下载到临时文件。

        Returns:
            `AlignmentReport`（aligned / unaligned / mismatch + warnings）。

        Notes:
            · 只处理**转换产物**（`converter_version` 非空）——原文就是真实 PDF 的记录不重算（⑥-3）；
            · 单据缺失/未完成时返回带 warning 的空报告，**不抛业务异常**（⑥-2）。
        """
        record = await self._previews.get_by_contract(contract_id)
        if (
            record is None
            or record.status is not PreviewStatus.done
            or record.converter_version is None
            or not record.preview_object_key
        ):
            return AlignmentReport(
                contract_id=str(contract_id),
                warnings=("PREVIEW_NOT_FROM_CONVERSION",),
            )

        if preview_pdf is not None:
            report = await self._align_with_pdf(contract_id, Path(preview_pdf))
        else:
            work_dir = Path(tempfile.mkdtemp(prefix=f"cg-align-{contract_id}-"))
            try:
                target = work_dir / "preview.pdf"
                self._context.object_storage.download_object(
                    record.preview_object_key,
                    str(target),
                )
                report = await self._align_with_pdf(contract_id, target)
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
        log_event(
            _LOG,
            "preview.align",
            level="INFO" if report.aligned else "WARNING",
            contract_id=str(contract_id),
            aligned=report.aligned,
            unaligned=report.unaligned,
            mismatch=report.mismatch,
        )
        return report

    async def _align_with_pdf(
        self,
        contract_id: UUID,
        pdf_path: Path,
    ) -> AlignmentReport:
        """抽取行 → 建索引 → 逐条对齐 → 交 `IngestionService` 落库。"""
        warnings: list[str] = []
        try:
            lines, page_sizes = extract_preview_lines(pdf_path)
        except Exception as exc:  # noqa: BLE001 - 抽行失败不能把预览读路径打成 500
            return AlignmentReport(
                contract_id=str(contract_id),
                warnings=(f"PREVIEW_PDF_UNREADABLE:{type(exc).__name__}",),
            )
        if not lines:
            return AlignmentReport(
                contract_id=str(contract_id),
                warnings=("PREVIEW_PDF_NO_TEXT_LINES",),
            )

        aligner = build_bbox_aligner()
        aligner.index_text_offsets(lines)
        chunks = await self._elements.list_by_contract(contract_id)
        updated, aligned, unaligned, mismatch = align_chunks_to_lines(
            chunks,
            lines=lines,
            aligner=aligner,
            page_sizes=page_sizes,
            warnings=warnings,
        )
        await self._ingestion.finalize_preview_alignment(contract_id, updated)
        return AlignmentReport(
            contract_id=str(contract_id),
            aligned=aligned,
            unaligned=unaligned,
            mismatch=mismatch,
            warnings=tuple(warnings),
        )

    async def _require_contract(
        self,
        contract_id: UUID,
        user: UserRecord | None = None,
    ) -> ContractDetail:
        """读取合同；不存在/越权都抛 `ContractNotFoundError`（路由统一映射 404）。"""
        owner_id, include_all = owner_scope(user)
        if owner_id is None:
            contract = await self._contracts.get(contract_id)
        else:
            contract = await self._contracts.get(
                contract_id,
                owner_id=owner_id,
                include_all=include_all,
            )
        if contract is None:
            raise ContractNotFoundError(str(contract_id))
        return contract

    async def _initialize(self, contract: ContractDetail) -> PreviewRecord:
        """按来源类型建立初始预览行（PDF 立即 DONE）。"""
        status = initial_preview_status(contract.filename)
        return await self._previews.create(
            contract.id,
            status=status,
            preview_object_key=preview_object_key_for(contract),
            error_message=(
                "ERR_UNSUPPORTED_PREVIEW_SOURCE"
                if status is PreviewStatus.failed
                else None
            ),
        )

    def _to_preview_data(self, record: PreviewRecord) -> PreviewData:
        """投影为对外 DTO：**只**输出短期签名 URL，绝不输出对象 key。"""
        preview_url: str | None = None
        expires_in = 0
        if record.status is PreviewStatus.done and record.preview_object_key:
            preview_url = self._context.object_storage.presign_get(
                record.preview_object_key,
                PREVIEW_URL_TTL_SECONDS,
            )
            expires_in = PREVIEW_URL_TTL_SECONDS
        return PreviewData(
            contractId=str(record.contract_id),
            status=record.status,
            previewUrl=preview_url,
            expiresIn=expires_in,
            pageCount=record.page_count,
            converterVersion=record.converter_version,
            createdAt=record.created_at,
        )

    @staticmethod
    def _to_progress(
        record: PreviewRecord,
        status: Status,
        *,
        stage: str,
        percent: float,
    ) -> TaskProgress:
        """把预览行投影为任务进度（`taskId` 为在飞 Celery id）。"""
        return TaskProgress(
            taskId=record.convert_task_id or str(record.id),
            stage=stage,
            percent=percent,
            status=status,
            result={
                "contractId": str(record.contract_id),
                "previewStatus": record.status.value,
            },
        )


__all__ = [
    "PREVIEW_URL_TTL_SECONDS",
    "SOURCE_DOCX",
    "SOURCE_IMAGE",
    "SOURCE_PDF",
    "PreviewService",
    "initial_preview_status",
    "preview_object_key_for",
    "source_kind_of",
]
