"""C60 预览坐标重建与 BBox 映射测试（离线）。

纯函数（`align_chunks_to_lines` / `clamp_bbox` / `find_text_span`）用合成行覆盖
「匹配 / 不匹配 / 坐标系 / 幂等 / 越界夹取」；服务层用替身覆盖「只重建转换产物」。
真实 PDF 抽取（pdfplumber）不在离线用例内，由 `extract_preview_lines` 的调用方替换。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository.preview import PreviewRecord
from app.generated.schemas import PreviewStatus
from app.services import preview_service as preview_module
from app.services.context import ServiceContext
from app.services.ingestion import clause_payload_to_chunk
from app.services.ports import ProgressBrokerPort
from app.services.preview_service import (
    AlignmentReport,
    PreviewService,
    _normalize_with_map,
    align_chunks_to_lines,
    clamp_bbox,
    find_text_span,
    union_bbox,
)
from app.services.projector import ContractProjector
from doc_processor.aligner import build_bbox_aligner
from doc_processor.parser import TextLine
from rag_contracts.chunk import BBOX_COORDINATE_SYSTEM, BBox, ContractChunk

CONTRACT_ID = "00000000-0000-0000-0000-0000000000d1"
CONTRACT_UUID = UUID(CONTRACT_ID)
PAGE_SIZES: dict[int, tuple[float, float]] = {1: (595.0, 842.0), 2: (595.0, 842.0)}

CLAUSE_ONE = "第一条 本合同由甲方与乙方签订"
CLAUSE_TWO = "第二条 付款期限为三十日"
CLAUSE_THREE = "第三条 争议提交仲裁委员会"
CLAUSE_MISSING = "第九条 本条款只存在于原文档"


def _lines() -> list[TextLine]:
    """合成预览 PDF 行：字符偏移口径 = `"\\n".join(行文本)`（与 3-6 一致）。"""
    rows = [
        (1, CLAUSE_ONE, 72.0, 100.0, 200.0, 12.0),
        (1, CLAUSE_TWO, 72.0, 130.0, 180.0, 12.0),
        (2, CLAUSE_THREE, 72.0, 100.0, 150.0, 12.0),
    ]
    lines: list[TextLine] = []
    cursor = 0
    for page, text, x, y, width, height in rows:
        lines.append(
            TextLine(
                page=page,
                text=text,
                x=x,
                y=y,
                width=width,
                height=height,
                char_start=cursor,
                char_end=cursor + len(text),
            )
        )
        cursor += len(text) + 1  # 行间分隔符 "\\n" 不计入任何行
    return lines


def _aligner(lines: Sequence[TextLine]):
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(list(lines))
    return aligner


def _chunk(
    clause_id: str,
    text: str,
    *,
    seq: int = 1,
    bbox: BBox | None = None,
) -> ContractChunk:
    chunk = clause_payload_to_chunk(
        CONTRACT_ID,
        {"clause_id": clause_id, "text": text},
        seq,
    )
    if bbox is not None:
        chunk = chunk.model_copy(update={"bbox": bbox, "page_no": bbox.page})
    return chunk


# --------------------------------------------------------------------------
# 1. 断言 1：可匹配条款获得正确页 / BBox
# --------------------------------------------------------------------------
def test_preview_bbox_rebuild() -> None:
    """条款文本能在预览 PDF 行中对齐 → 页码与并集框都来自对齐结果。"""
    lines = _lines()
    chunks = [_chunk("A-1", CLAUSE_TWO, seq=1)]

    updated, aligned, unaligned, mismatch = align_chunks_to_lines(
        chunks,
        lines=lines,
        aligner=_aligner(lines),
        page_sizes=PAGE_SIZES,
    )

    assert (aligned, unaligned, mismatch) == (1, 0, 0)
    box = updated[0].bbox
    assert box is not None
    assert box.page == 1
    assert (box.x, box.y, box.width, box.height) == (72.0, 130.0, 180.0, 12.0)
    assert updated[0].page_no == 1
    # 其余字段与 chunk_id 原样保留（幂等 upsert 的前提）
    assert updated[0].chunk_id == chunks[0].chunk_id
    assert updated[0].text == chunks[0].text


def test_preview_bbox_rebuild_cross_page() -> None:
    """第二条在页 2：页码随对齐结果走，不沿用第一条的页号。"""
    lines = _lines()
    updated, aligned, *_ = align_chunks_to_lines(
        [_chunk("A-3", CLAUSE_THREE, seq=3)],
        lines=lines,
        aligner=_aligner(lines),
        page_sizes=PAGE_SIZES,
    )

    assert aligned == 1
    assert updated[0].bbox is not None
    assert updated[0].bbox.page == 2
    assert updated[0].page_no == 2


# --------------------------------------------------------------------------
# 2. 断言 2：不可匹配为 null
# --------------------------------------------------------------------------
def test_unmatched_bbox_is_null() -> None:
    """PDF 中找不到的条款保持 `bbox=None` / `page_no=None`，并写 warning。"""
    lines = _lines()
    warnings: list[str] = []

    updated, aligned, unaligned, mismatch = align_chunks_to_lines(
        [_chunk("A-9", CLAUSE_MISSING, seq=9)],
        lines=lines,
        aligner=_aligner(lines),
        page_sizes=PAGE_SIZES,
        warnings=warnings,
    )

    assert (aligned, unaligned, mismatch) == (0, 1, 0)
    assert updated[0].bbox is None
    assert updated[0].page_no is None
    assert warnings and "A-9" in warnings[0]


def test_unmatched_clears_stale_synthetic_bbox() -> None:
    """已有**合成**坐标但无法对齐时清空（绝不把 DOCX 合成坐标当真实页面坐标）。"""
    lines = _lines()
    stale = BBox(page=1, x=0.0, y=0.0, width=0.0, height=15.0)

    updated, *_ = align_chunks_to_lines(
        [_chunk("A-9", CLAUSE_MISSING, seq=9, bbox=stale)],
        lines=lines,
        aligner=_aligner(lines),
        page_sizes=PAGE_SIZES,
    )

    assert updated[0].bbox is None
    assert updated[0].page_no is None


def test_missing_bbox_no_fake_highlight() -> None:
    """缺坐标 / 退化框一律不投影 bbox（前端拿不到框 → 不画假高亮）。"""
    lines = _lines()
    updated, *_ = align_chunks_to_lines(
        [_chunk("A-9", CLAUSE_MISSING, seq=9)],
        lines=lines,
        aligner=_aligner(lines),
        page_sizes=PAGE_SIZES,
    )

    assert ContractProjector.elements([updated[0]])[0].bbox is None

    degenerate = updated[0].model_copy(
        update={"bbox": BBox(page=1, x=10.0, y=10.0, width=0.0, height=0.0)}
    )
    assert ContractProjector.elements([degenerate])[0].bbox is None

    good = updated[0].model_copy(
        update={"bbox": BBox(page=2, x=1.0, y=2.0, width=3.0, height=4.0)}
    )
    projected = ContractProjector.elements([good])[0].bbox
    assert projected is not None
    assert projected.coordinate_system.value == "PDF_POINTS"
    assert projected.page == 2


def test_short_clause_text_is_not_matched() -> None:
    """过短文本（易误配）不参与对齐：直接判为不可匹配。"""
    lines = _lines()
    normalized, mapping = _normalize_with_map("\n".join(line.text for line in lines))

    assert find_text_span("甲方", normalized=normalized, mapping=mapping) is None
    assert (
        find_text_span(CLAUSE_TWO, normalized=normalized, mapping=mapping) is not None
    )


def test_match_tolerates_whitespace_differences() -> None:
    """DOCX 与 PDF 的换行/缩进差异由归一化吸收，定位仍回到原始偏移。"""
    lines = _lines()
    normalized, mapping = _normalize_with_map("\n".join(line.text for line in lines))

    span = find_text_span(
        "第二条   付款期限为三十日\n", normalized=normalized, mapping=mapping
    )

    assert span is not None
    assert normalized[span[0] : span[1]] == CLAUSE_TWO


# --------------------------------------------------------------------------
# 3. 断言 3：坐标系统为 PDF_POINTS（1-based page）
# --------------------------------------------------------------------------
def test_bbox_coordinate_system() -> None:
    """重建出的框统一 `PDF_POINTS`，页码从 1 开始。"""
    lines = _lines()
    chunks = [_chunk("A-1", CLAUSE_ONE, seq=1), _chunk("A-3", CLAUSE_THREE, seq=3)]

    updated, aligned, unaligned, mismatch = align_chunks_to_lines(
        chunks,
        lines=lines,
        aligner=_aligner(lines),
        page_sizes=PAGE_SIZES,
    )

    assert (aligned, unaligned, mismatch) == (2, 0, 0)
    for chunk in updated:
        assert chunk.bbox is not None
        assert chunk.bbox.coordinate_system == BBOX_COORDINATE_SYSTEM == "PDF_POINTS"
        assert chunk.bbox.page >= 1
        assert chunk.page_no == chunk.bbox.page


# --------------------------------------------------------------------------
# 4. 断言 4：重复重建幂等
# --------------------------------------------------------------------------
def test_rebuild_idempotent() -> None:
    """同一份 PDF 重复重建：坐标完全相同、chunk_id 不变（upsert 幂等）。"""
    lines = _lines()
    chunks = [
        _chunk("A-1", CLAUSE_ONE, seq=1),
        _chunk("A-2", CLAUSE_TWO, seq=2),
        _chunk("A-9", CLAUSE_MISSING, seq=9),
    ]

    first, *_ = align_chunks_to_lines(
        chunks, lines=lines, aligner=_aligner(lines), page_sizes=PAGE_SIZES
    )
    second, *_ = align_chunks_to_lines(
        first, lines=lines, aligner=_aligner(lines), page_sizes=PAGE_SIZES
    )

    assert [chunk.chunk_id for chunk in second] == [chunk.chunk_id for chunk in chunks]
    assert [chunk.bbox for chunk in second] == [chunk.bbox for chunk in first]
    assert [chunk.page_no for chunk in second] == [chunk.page_no for chunk in first]


def test_unbounded_bbox_clamped() -> None:
    """越界框夹取到页面矩形内；完全越界退化为零面积（随后不投影）。"""
    clamped = clamp_bbox(
        BBox(page=1, x=580.0, y=830.0, width=100.0, height=100.0),
        (595.0, 842.0),
    )
    assert (clamped.x, clamped.y) == (580.0, 830.0)
    assert (clamped.width, clamped.height) == (15.0, 12.0)

    outside = clamp_bbox(
        BBox(page=1, x=700.0, y=900.0, width=10.0, height=10.0),
        (595.0, 842.0),
    )
    assert (outside.width, outside.height) == (0.0, 0.0)

    # 行框本身越界时，并集框同样被夹取
    boxes = [
        type(
            "Box",
            (),
            {"page": 1, "x": 590.0, "y": 840.0, "width": 50.0, "height": 50.0},
        )()
    ]
    union = union_bbox(boxes, 1, (595.0, 842.0))
    assert union is not None
    assert union.width == 5.0 and union.height == 2.0


# --------------------------------------------------------------------------
# 5. 服务层：只重建「转换产物」，且写回走 IngestionService
# --------------------------------------------------------------------------
class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeObjectStorage:
    def __init__(self, pdf_bytes: bytes = b"%PDF-1.4 fake") -> None:
        self.pdf_bytes = pdf_bytes
        self.downloads: list[tuple[str, str]] = []

    def presign_put(self, file_key: str, expires_s: int = 600) -> str:
        raise AssertionError("坐标重建不签发 PUT URL")

    def presign_get(self, file_key: str, expires_s: int = 900) -> str:
        return f"https://minio.test/{file_key}"

    def stat_object(self, file_key: str) -> Any:
        raise AssertionError("坐标重建不读 stat")

    def download_object(self, file_key: str, target_path: str) -> str:
        self.downloads.append((file_key, target_path))
        Path(target_path).write_bytes(self.pdf_bytes)
        return target_path


class FakePreviewRepository:
    def __init__(self, record: PreviewRecord | None) -> None:
        self._record = record

    async def get_by_contract(self, contract_id: UUID) -> PreviewRecord | None:
        if self._record is not None and self._record.contract_id == contract_id:
            return self._record
        return None


class FakeElementRepository:
    def __init__(self, chunks: Sequence[ContractChunk]) -> None:
        self.chunks = list(chunks)

    async def list_by_contract(self, contract_id: UUID) -> list[ContractChunk]:
        del contract_id
        return list(self.chunks)


class FakeIngestion:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, list[ContractChunk]]] = []

    async def finalize_preview_alignment(
        self,
        contract_id: UUID,
        chunks: Sequence[ContractChunk],
    ) -> int:
        self.calls.append((contract_id, list(chunks)))
        return len(chunks)


def _preview_record(*, converter_version: str | None) -> PreviewRecord:
    now = datetime.now(UTC)
    return PreviewRecord(
        id=UUID("00000000-0000-0000-0000-0000000000e1"),
        contract_id=CONTRACT_UUID,
        status=PreviewStatus.done,
        preview_object_key="contract_previews/c-1/converted.pdf",
        convert_task_id="celery-1",
        page_count=None,
        converter_version=converter_version,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _service(
    *,
    record: PreviewRecord | None,
    chunks: Sequence[ContractChunk] = (),
    storage: FakeObjectStorage | None = None,
):
    storage = storage or FakeObjectStorage()
    ingestion = FakeIngestion()
    context = ServiceContext(
        session=cast(AsyncSession, FakeSession()),
        object_storage=storage,
        task_dispatcher=cast(Any, object()),
        progress_broker=cast(ProgressBrokerPort, object()),
    )
    service = PreviewService(
        context,
        previews=cast(Any, FakePreviewRepository(record)),
        contracts=cast(Any, object()),
        elements=cast(Any, FakeElementRepository(chunks)),
        ingestion=cast(Any, ingestion),
    )
    return service, storage, ingestion


def test_rebuild_skips_native_pdf_preview() -> None:
    """原文就是真实 PDF（无 converter_version）→ 不重算、不写库。"""
    service, storage, ingestion = _service(
        record=_preview_record(converter_version=None),
        chunks=[_chunk("A-1", CLAUSE_ONE)],
    )

    report = asyncio.run(service.rebuild_preview_bbox(CONTRACT_UUID))

    assert report == AlignmentReport(
        contract_id=CONTRACT_ID,
        warnings=("PREVIEW_NOT_FROM_CONVERSION",),
    )
    assert storage.downloads == []
    assert ingestion.calls == []


def test_rebuild_downloads_converted_pdf_and_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """转换产物：下载派生 PDF → 对齐 → 交 `IngestionService` 写回。"""
    lines = _lines()
    storage = FakeObjectStorage()
    monkeypatch.setattr(
        preview_module,
        "extract_preview_lines",
        lambda _path: (lines, PAGE_SIZES),
    )
    service, storage, ingestion = _service(
        record=_preview_record(converter_version="libreoffice-24.8"),
        chunks=[_chunk("A-2", CLAUSE_TWO, seq=2)],
        storage=storage,
    )

    report = asyncio.run(service.rebuild_preview_bbox(CONTRACT_UUID))

    assert (report.aligned, report.unaligned, report.mismatch) == (1, 0, 0)
    assert storage.downloads[0][0] == "contract_previews/c-1/converted.pdf"
    assert ingestion.calls and ingestion.calls[0][0] == CONTRACT_UUID
    written = ingestion.calls[0][1][0]
    assert written.bbox is not None and written.bbox.page == 1


def test_rebuild_accepts_explicit_pdf_path(tmp_path: Path) -> None:
    """显式传入 PDF 路径时不下载（§⑤ 的 `preview_pdf` 参数）。"""
    lines = _lines()
    pdf = tmp_path / "preview.pdf"
    pdf.write_bytes(b"%PDF-1.4 local")
    service, storage, ingestion = _service(
        record=_preview_record(converter_version="libreoffice-24.8"),
        chunks=[_chunk("A-1", CLAUSE_ONE)],
    )

    with pytest.MonkeyPatch.context() as monkey:
        monkey.setattr(
            preview_module,
            "extract_preview_lines",
            lambda _path: (lines, PAGE_SIZES),
        )
        report = asyncio.run(service.rebuild_preview_bbox(CONTRACT_UUID, pdf))

    assert report.aligned == 1
    assert storage.downloads == []
    assert len(ingestion.calls) == 1


def test_rebuild_unreadable_pdf_is_warning_not_error() -> None:
    """抽行失败只记 warning，不抛业务异常（⑥-2）。"""
    service, _, ingestion = _service(
        record=_preview_record(converter_version="libreoffice-24.8"),
        chunks=[_chunk("A-1", CLAUSE_ONE)],
        storage=FakeObjectStorage(pdf_bytes=b"not-a-pdf"),
    )

    report = asyncio.run(service.rebuild_preview_bbox(CONTRACT_UUID))

    assert report.aligned == 0
    assert report.warnings and report.warnings[0].startswith("PREVIEW_PDF_UNREADABLE")
    assert ingestion.calls == []
