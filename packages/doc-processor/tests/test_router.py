"""文档分流器自检（T-C1 / `3-2 §⑧`）。

夹具由 `scripts/make_ocr_fixtures.py` 生成并**随仓库提交**（离线可跑，无需网络）。

覆盖：
1. 四路分流各一条（DOCX / PDF_NATIVE / PDF_SCANNED / IMAGE）；
2. **阈值必须被消费**（同一个文件、不同阈值 → 不同支路）；
3. 异常分支：不支持扩展名、不存在路径、**损坏 PDF**（不得静默当扫描件）；
4. 采样算法：页数不足时全取、多页时首/中/末且去重升序；
5. `reason` 必须含数字（可解释）；`DocRouter` 无状态（连续调用结果一致）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_processor.config import OcrSettings
from doc_processor.errors import UnsupportedFormatError
from doc_processor.router import DocRouter, RouteKind

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> Path:
    """取夹具路径；缺失时直接失败（夹具应随仓库提交）。"""
    path = _FIXTURES / name
    assert path.is_file(), f"夹具缺失：{path}（先跑 scripts/make_ocr_fixtures.py）"
    return path


def _settings(**overrides: object) -> OcrSettings:
    """构造离线配置（`_env_file=None` 避免 .env 干扰）。"""
    base: dict[str, object] = {"native_text_min_chars": 50, "route_sample_pages": 3}
    base.update(overrides)
    return OcrSettings(_env_file=None, **base)


# ===========================================================================
# 1. 四路分流
# ===========================================================================
def test_docx_routes_to_docx() -> None:
    """§⑦ 断言 1：DOCX 直抽，不做无意义的文本层采样。"""
    decision = DocRouter().route(_fixture("contract.docx"))
    assert decision.kind is RouteKind.DOCX
    assert decision.sampled_pages == [1]


def test_native_pdf_routes_to_native() -> None:
    """§⑦ 断言 2：数字版 PDF 直抽；采样页为首/中/末且升序去重。"""
    decision = DocRouter().route(_fixture("native.pdf"))
    assert decision.kind is RouteKind.PDF_NATIVE
    assert decision.page_count == 5
    assert decision.sampled_pages == [1, 3, 5]
    assert decision.avg_chars_per_page >= 50


def test_scanned_pdf_routes_to_ocr() -> None:
    """§⑦ 断言 3：无文本层 PDF 走 OCR。"""
    decision = DocRouter().route(_fixture("scanned.pdf"))
    assert decision.kind is RouteKind.PDF_SCANNED
    assert decision.avg_chars_per_page == 0.0


def test_image_routes_to_image() -> None:
    """§⑦ 断言 4：图片直接 OCR，页数为 1。"""
    decision = DocRouter().route(_fixture("scan.png"))
    assert decision.kind is RouteKind.IMAGE
    assert decision.page_count == 1
    assert decision.sampled_pages == [1]


# ===========================================================================
# 2. 阈值必须被消费（防"声明了没人读"）
# ===========================================================================
def test_threshold_is_consumed() -> None:
    """§⑦ 断言 5：同一个数字版 PDF，抬高阈值后应判为扫描件。"""
    native = _fixture("native.pdf")
    assert DocRouter(_settings()).route(native).kind is RouteKind.PDF_NATIVE
    assert (
        DocRouter(_settings(native_text_min_chars=1_000_000)).route(native).kind
        is RouteKind.PDF_SCANNED
    )


def test_sample_size_is_consumed() -> None:
    """采样上限同样被消费：上限 ≥ 页数时全采，否则取「首 / 中 / 末」（3-2 §⑥-1 唯一处算法）。"""
    native = _fixture("native.pdf")
    assert DocRouter(_settings(route_sample_pages=5)).route(native).sampled_pages == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert DocRouter(_settings(route_sample_pages=3)).route(native).sampled_pages == [1, 3, 5]


# ===========================================================================
# 3. 异常分支
# ===========================================================================
def test_unsupported_extension_raises(tmp_path: Path) -> None:
    """§⑦ 断言 6：`.txt` / 无扩展名 / 不存在的路径都要明确报错。"""
    txt = tmp_path / "note.txt"
    txt.write_text("hello", encoding="utf-8")
    plain = tmp_path / "noext"
    plain.write_text("hello", encoding="utf-8")

    for bad in (txt, plain, tmp_path / "missing.pdf"):
        with pytest.raises(UnsupportedFormatError):
            DocRouter().route(bad)


def test_corrupt_pdf_raises_not_scanned(tmp_path: Path) -> None:
    """损坏 PDF 必须报错 —— **不得**静默降级为 PDF_SCANNED（否则白跑几分钟 OCR）。"""
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4 not really a pdf" + b"\x00" * 64)
    with pytest.raises(UnsupportedFormatError):
        DocRouter().route(corrupt)


# ===========================================================================
# 4. 采样算法与可解释性
# ===========================================================================
def test_sample_pages_when_fewer_pages() -> None:
    """单页 PDF → 采样页 [1]，且不出现除零。"""
    decision = DocRouter().route(_fixture("scanned.pdf"))
    assert decision.sampled_pages == [1]


def test_reason_contains_numbers() -> None:
    """`reason` 必须含关键数字（字符数 / 阈值 / 页数），供排查"为什么走了 OCR"。"""
    for name in ("native.pdf", "scanned.pdf"):
        reason = DocRouter().route(_fixture(name)).reason
        assert any(ch.isdigit() for ch in reason), reason
        assert "阈值" in reason


def test_router_is_stateless() -> None:
    """连续两次 `route()` 结果一致（无状态）。"""
    router = DocRouter()
    first = router.route(_fixture("native.pdf"))
    second = router.route(_fixture("native.pdf"))
    assert first == second
