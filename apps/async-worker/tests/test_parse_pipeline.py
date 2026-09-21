"""解析流水线自检（T-C7 / `3-6 §⑧`）。

覆盖：
1. 装配（字符偏移唯一处：分隔符 `\n`、空行不占偏移、半开区间）；
2. 接缝（条款 payload 键名、bbox 形状、span 与 AST 不一致时置空而不猜）；
3. 支路（DOCX 合成坐标、扫描件页数上限、引擎不可用降级、印章模块缺失只告警一次）；
4. 源文件定位（本地/显式路径/MinIO 三通道）与工件原子写；
5. 红线：不写第二处缩放、不写行聚类。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_processor.config import OcrSettings
from worker.parse_pipeline import (
    ParseOutcome,
    SourceUnavailableError,
    assemble_text_lines,
    build_parse_pipeline,
    clause_payloads,
    resolve_source,
    write_parse_artifact,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests → async-worker → apps → 仓库根
_FIXTURES = _REPO_ROOT / "packages" / "doc-processor" / "tests" / "fixtures"


def _settings(**overrides: object) -> OcrSettings:
    """构造离线配置（避免 .env 干扰）。"""
    base: dict[str, object] = {"engine": "rapid", "render_dpi": 200, "image_dpi": 72, "timeout_s": 30}
    base.update(overrides)
    return OcrSettings(_env_file=None, **base)


# ===========================================================================
# 1. 装配（唯一处）
# ===========================================================================
def test_assemble_offsets_monotonic() -> None:
    """分隔符 `\n`、空行不占偏移、`len(text) == 末行 char_end`。"""
    rows = [
        (1, "abc", 0.0, 0.0, 10.0, 10.0),
        (1, "   ", 0.0, 0.0, 0.0, 0.0),
        (1, "de", 0.0, 20.0, 10.0, 10.0),
    ]
    lines, text = assemble_text_lines(rows)
    assert len(lines) == 2
    assert text == "abc\nde"
    assert lines[0].char_end + 1 == lines[1].char_start
    assert len(text) == lines[-1].char_end


def test_assemble_empty_rows_is_empty() -> None:
    """全空行 → 空结果（不抛异常）。"""
    lines, text = assemble_text_lines([(1, "", 0.0, 0.0, 0.0, 0.0)])
    assert lines == [] and text == ""


# ===========================================================================
# 2. 接缝：条款 payload
# ===========================================================================
def _clauses_of(contract_path: Path) -> tuple[list, list, object]:
    """抽条款 + 装配行 + 建索引（返回三元组，供接缝用例复用）。"""
    from doc_processor.aligner import build_bbox_aligner
    from doc_processor.parser import build_node_extractor

    extractor = build_node_extractor(_settings())
    lines = extractor._lines_from_path(str(contract_path))
    clauses = extractor.clauses_from_lines(lines)
    aligner = build_bbox_aligner()
    aligner.index_text_offsets(lines)
    return clauses, lines, aligner


def test_clause_payload_keys_and_bbox_shape() -> None:
    """条款 payload 键名与 bbox 形状（`3-6 §5.4` 的接缝表）。"""
    clauses, lines, aligner = _clauses_of(_FIXTURES / "native.pdf")
    payloads = clause_payloads(clauses, lines, aligner)
    assert payloads, "夹具应切出条款"
    first = payloads[0]
    assert set(first) == {
        "clause_id",
        "label",
        "text",
        "level",
        "parent_id",
        "page_no",
        "bbox",
        "children_ids",
    }
    assert first["bbox"] is not None
    assert set(first["bbox"]) == {"page", "x", "y", "width", "height", "coordinateSystem"}
    assert first["bbox"]["coordinateSystem"] == "PDF_POINTS"
    assert first["page_no"] == 1 and first["bbox"]["page"] == 1


def test_clause_payload_matches_adapter() -> None:
    """与 `2-6 to_clause_inputs()` 对齐（该模块未落盘时用键名断言兜住）。"""
    clauses, lines, aligner = _clauses_of(_FIXTURES / "native.pdf")
    payloads = clause_payloads(clauses, lines, aligner)
    adapters = pytest.importorskip(
        "worker.adapters", reason="worker/adapters.py 属 T-B8（2-6），未落盘时只做键名断言"
    )
    inputs = adapters.to_clause_inputs(payloads)
    assert len(inputs) == len(payloads)


def test_clause_span_mismatch_sets_bbox_none() -> None:
    """行区间与 AST 条数/label 不一致 → 该条款 `page_no`/`bbox` 置空 + 一条告警。"""
    clauses, lines, aligner = _clauses_of(_FIXTURES / "native.pdf")
    # 人为把第 2 条条款的 label 改坏，模拟"两套切分对不上"
    clauses[1].label = "第X条"
    warnings: list[str] = []
    payloads = clause_payloads(clauses, lines, aligner, warnings=warnings)
    assert payloads[1]["bbox"] is None and payloads[1]["page_no"] is None
    assert any("无法与行区间对齐" in item for item in warnings)


# ===========================================================================
# 3. 支路行为
# ===========================================================================
def test_pdf_native_pipeline_end_to_end() -> None:
    """数字版 PDF：四类产出齐全，且状态只因印章模块缺失而降级。"""
    outcome = build_parse_pipeline(settings=_settings()).run(
        _FIXTURES / "native.pdf", "c-native"
    )
    payload = outcome.to_payload()
    assert payload["fileType"] == "PDF_NATIVE"
    assert payload["status"] == "DEGRADED"  # 3-8 未落盘 → 按状态表降级
    assert payload["lineCount"] > 0 and payload["charCount"] > 0
    assert payload["clauses"] and payload["layoutBlocks"]
    assert payload["coordinatesSynthetic"] is False
    assert payload["ocrUsed"] is False
    assert any("印章模块" in item for item in payload["warnings"])
    assert {entry["stage"] for entry in payload["stageLog"]} >= {
        "route",
        "parse_text",
        "seal",
        "extract",
        "index",
    }


def test_docx_coordinates_synthetic() -> None:
    """DOCX：合成坐标 + 降级 + 无精确 bbox（契约要求"缺坐标就是 None"）。"""
    outcome = build_parse_pipeline(settings=_settings()).run(
        _FIXTURES / "contract.docx", "c-docx"
    )
    payload = outcome.to_payload()
    assert payload["fileType"] == "DOCX"
    assert payload["status"] == "DEGRADED"
    assert payload["coordinatesSynthetic"] is True
    assert all(clause["bbox"] is None for clause in payload["clauses"])
    assert payload["sealDetections"] == []
    assert any("DOCX 无坐标真值" in item for item in payload["warnings"])


def test_scanned_pdf_truncates_at_max_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫描件页数超上限 → 截断 + 降级告警（**不静默丢页**）。"""
    import fitz

    import worker.parse_pipeline as pipeline_module

    # 造一份 2 页、无文本层的扫描件（把 native.pdf 的前两页栅格化）
    source = fitz.open(str(_FIXTURES / "native.pdf"))
    scanned = fitz.open()
    for index in range(2):
        pix = source[index].get_pixmap(dpi=100)
        page = scanned.new_page(width=pix.width * 72 / 100, height=pix.height * 72 / 100)
        page.insert_image(page.rect, pixmap=pix)
    target = tmp_path / "two_pages_scanned.pdf"
    scanned.save(str(target))
    scanned.close()
    source.close()

    monkeypatch.setattr(pipeline_module, "MAX_OCR_PAGES", 1)
    outcome = pipeline_module.build_parse_pipeline(settings=_settings()).run(target, "c-trunc")
    payload = outcome.to_payload()
    assert payload["fileType"] == "PDF_SCANNED"
    assert payload["ocrUsed"] is True
    assert payload["pageCount"] == 2
    assert any("超过上限" in item for item in payload["warnings"])


def test_engine_unavailable_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎不可用 → DEGRADED（不 FAILED、不抛异常），文本为空。"""
    import doc_processor.ocr.engine as engine_module
    from doc_processor.errors import OcrEngineUnavailableError

    def _boom(settings: object = None) -> None:
        raise OcrEngineUnavailableError("rapid", "测试注入")

    monkeypatch.setattr(engine_module, "build_ocr_engine", _boom)
    outcome = build_parse_pipeline(settings=_settings()).run(
        _FIXTURES / "scanned.pdf", "c-noengine"
    )
    payload = outcome.to_payload()
    assert payload["status"] == "DEGRADED"
    assert payload["lineCount"] == 0
    assert any("OCR 引擎不可用" in item for item in payload["warnings"])


def test_seal_module_missing_warns_once() -> None:
    """印章模块缺失：多页 PDF 也只产生**一条** warning（不逐页刷屏）。"""
    outcome = build_parse_pipeline(settings=_settings()).run(
        _FIXTURES / "native.pdf", "c-sealwarn"
    )
    seal_warnings = [item for item in outcome.warnings if "印章模块" in item]
    assert len(seal_warnings) == 1


# ===========================================================================
# 4. 源文件定位与工件
# ===========================================================================
def test_resolve_source_local_path_wins() -> None:
    """显式本地路径优先级最高。"""
    resolved = resolve_source("ignored-key", str(_FIXTURES / "native.pdf"))
    assert resolved == _FIXTURES / "native.pdf"


def test_resolve_source_explicit_missing_raises() -> None:
    """显式给了本地路径却不存在 → 抛错（**不静默回落到 MinIO**）。"""
    with pytest.raises(SourceUnavailableError):
        resolve_source("ignored-key", "no-such-file.pdf")


def test_resolve_source_unavailable_without_minio(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地路径不存在且 MinIO 不可用 → `SourceUnavailableError`（不猜桶名）。"""

    class _BrokenClient:
        def fget_object(self, bucket: str, key: str, path: str) -> None:
            raise OSError("minio down")

    monkeypatch.setattr("worker.storage.get_minio_client", lambda: _BrokenClient(), raising=False)
    with pytest.raises(SourceUnavailableError) as excinfo:
        resolve_source("contracts/missing.pdf")
    assert "MinIO" in str(excinfo.value) or "minio" in str(excinfo.value).lower()


def test_resolve_source_minio_download_mocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MinIO 通道：下载到本地临时文件后返回该路径。"""

    class _FakeClient:
        def fget_object(self, bucket: str, key: str, path: str) -> None:
            Path(path).write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr("worker.storage.get_minio_client", lambda: _FakeClient(), raising=False)
    resolved = resolve_source("contracts/abc.pdf")
    assert resolved.is_file()
    assert resolved.name == "abc.pdf"


def test_artifact_atomic_write(tmp_path: Path) -> None:
    """工件写盘：JSON 可解析、无 `.tmp` 残留；目录不可写时返回 None 但不炸。"""
    outcome = ParseOutcome(contract_id="c-artifact", status="SUCCESS")
    written = write_parse_artifact(outcome, tmp_path)
    assert written is not None and written.is_file()
    assert not (tmp_path / "c-artifact.json.tmp").exists()
    import json

    assert json.loads(written.read_text(encoding="utf-8"))["contractId"] == "c-artifact"


def test_artifact_failure_does_not_raise(tmp_path: Path) -> None:
    """目录不可写 → 返回 None + 追加 warning（不影响任务状态）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    outcome = ParseOutcome(contract_id="c-blocked", status="SUCCESS")
    assert write_parse_artifact(outcome, blocker / "sub") is None
    assert any("工件写入失败" in item for item in outcome.warnings)


# ===========================================================================
# 5. 红线
# ===========================================================================
def test_no_scaling_no_clustering_in_pipeline() -> None:
    """本文件不得写第二处缩放或行聚类。

    ⚠️ 允许 `src_dpi=` —— 那只是把 DPI **透传**给 `3-3` 的唯一缩放处（`to_pdf_points`），
    不是在本模块里做换算（`3-6 §⑦ 断言 5` 的纠偏口径）。
    """
    source = (_REPO_ROOT / "apps" / "async-worker" / "worker" / "parse_pipeline.py").read_text(
        encoding="utf-8"
    )
    for banned in ("72 /", "72.0 /", "* 72", "extract_words"):
        assert banned not in source, f"禁止出现 {banned}"
