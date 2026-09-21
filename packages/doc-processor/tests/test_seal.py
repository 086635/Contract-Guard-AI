"""印章三段式自检（T-C6 / `3-8 §⑧`）。

覆盖：
1. 区域检测（双段红色掩膜、面积/长宽比/圆度筛选、候选上限、外扩）；
2. 极坐标展开（形状、退化框）；
3. 主体比对（精确/子串/标点归一/空输入）与两条判定规则表；
4. 检测器（引擎不可用降级、坐标归一、hints 提示与回落、payload 形状与枚举对齐）；
5. 红线（不产出 `MISSING`、不定义第二个枚举、契约冻结）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from doc_processor.config import OcrSettings
from doc_processor.ocr import SealDetection
from doc_processor.ocr.seal import (
    MAX_SEALS_PER_PAGE,
    LocalSealDetector,
    classify_seal_type,
    detect_seals_in_image,
    expand_ring,
    find_seal_regions,
    judge_status,
    match_parties,
    red_mask,
    to_detection_payload,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "packages" / "doc-processor" / "tests" / "fixtures"


def _settings(**overrides: object) -> OcrSettings:
    """构造离线配置（避免 .env 干扰）。"""
    base: dict[str, object] = {"render_dpi": 200, "image_dpi": 72, "min_confidence": 0.5}
    base.update(overrides)
    return OcrSettings(_env_file=None, **base)


def _page() -> np.ndarray:
    """600×800 白底页面。"""
    return np.full((800, 600, 3), 255, dtype=np.uint8)


def _red_circle(image: np.ndarray, center: tuple[int, int] = (300, 400), radius: int = 120) -> None:
    """画一枚红色圆环（模拟公章的红色轮廓）。"""
    import cv2

    cv2.circle(image, center, radius, (0, 0, 225), 14)


# ===========================================================================
# 1. 区域检测
# ===========================================================================
def test_red_mask_both_segments() -> None:
    """红色跨 0/180 两端：H≈5 与 H≈175 都必须命中（只写一段会漏一半红章）。"""
    import cv2

    hsv = np.zeros((10, 10, 3), dtype=np.uint8)
    hsv[:, :5] = (5, 255, 255)
    hsv[:, 5:] = (175, 255, 255)
    mask = red_mask(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR))
    assert (mask[:, :5] > 0).all()
    assert (mask[:, 5:] > 0).all()


def test_red_mask_ignores_blue_and_gray() -> None:
    """蓝/灰图像不得命中红色掩膜。"""
    image = np.full((50, 50, 3), 128, dtype=np.uint8)
    image[:25, :, :] = (255, 0, 0)  # BGR 蓝
    assert int((red_mask(image) > 0).sum()) == 0


def test_find_regions_circle() -> None:
    """圆章能被检出，且外接框近似方形。"""
    image = _page()
    _red_circle(image)
    regions = find_seal_regions(image, min_area_ratio=0.0005)
    assert len(regions) == 1
    x, y, width, height = regions[0]
    assert 0.8 <= width / height <= 1.25
    assert x < 300 < x + width and y < 400 < y + height  # 覆盖圆心


def test_find_regions_rejects_small_specks() -> None:
    """5×5 红点属于噪点，不产生候选。"""
    import cv2

    image = _page()
    cv2.rectangle(image, (100, 100), (105, 105), (0, 0, 225), -1)
    assert find_seal_regions(image, min_area_ratio=0.0005) == []


def test_find_regions_rejects_thin_strip() -> None:
    """细长红条（长宽比 5:1）不是印章。"""
    import cv2

    image = _page()
    cv2.rectangle(image, (50, 50), (450, 130), (0, 0, 225), -1)
    assert find_seal_regions(image, min_area_ratio=0.0005) == []


def test_find_regions_cap() -> None:
    """候选上限：单页最多 `MAX_SEALS_PER_PAGE` 枚（防红色商标刷爆）。"""
    image = _page()
    for index in range(MAX_SEALS_PER_PAGE + 4):
        _red_circle(image, center=(70 + (index % 5) * 110, 120 + (index // 5) * 130), radius=20)
    regions = find_seal_regions(image, min_area_ratio=0.0002)
    assert 0 < len(regions) <= MAX_SEALS_PER_PAGE


# ===========================================================================
# 2. 极坐标展开
# ===========================================================================
def test_expand_ring_shape() -> None:
    """展开条形状 = `(steps, 半径, 3)`（行 = 角度、列 = 半径）。"""
    image = _page()
    _red_circle(image)
    strip = expand_ring(image, (180, 280, 240, 240), steps=360)
    assert strip.shape[0] == 360 and strip.shape[2] == 3 and strip.size > 0


def test_expand_ring_degenerate_box() -> None:
    """退化框（宽或高为 0）→ 空数组，不抛异常。"""
    strip = expand_ring(_page(), (10, 10, 0, 0), steps=360)
    assert strip.size == 0


# ===========================================================================
# 3. 主体比对与判定规则
# ===========================================================================
def test_match_parties_exact_and_substring() -> None:
    """精确匹配与子串匹配都算命中；不相似则未命中。"""
    names = ["北京某某科技有限公司", "上海某某贸易有限公司"]
    assert match_parties("北京某某科技有限公司", names)[0] is True
    assert match_parties("某某科技有限公司", names)[0] is True  # 子串
    matched, ratio = match_parties("深圳别家公司", names)
    assert matched is False and ratio < 0.6


def test_match_parties_empty_inputs() -> None:
    """`text` 为空或主体名为空 → `(False, 0.0)`。"""
    assert match_parties(None, ["甲公司"]) == (False, 0.0)
    assert match_parties("甲公司", []) == (False, 0.0)


def test_match_parties_normalizes_punctuation() -> None:
    """归一化：去标点、全角转半角后仍能命中。"""
    assert match_parties("北京某某科技有限公司。", ["北京某某科技有限公司"])[0] is True
    assert match_parties("ABC", ["ＡＢＣ"])[0] is True


def test_classify_seal_type_table() -> None:
    """类型规则表：财务 / 合同 / 法人 / 默认公章。"""
    assert classify_seal_type("上海某某公司财务专用章") == "FINANCIAL_SEAL"
    assert classify_seal_type("合同专用章") == "CONTRACT_SEAL"
    assert classify_seal_type("法定代表人章") == "LEGAL_REPRESENTATIVE_SEAL"
    assert classify_seal_type("北京某某科技有限公司") == "OFFICIAL_SEAL"
    assert classify_seal_type(None) == "OFFICIAL_SEAL"


def test_judge_status_table() -> None:
    """状态规则表：重叠 > 不可辨 > 未授权 > 合规；未提供主体名时不判「未授权」。"""
    assert (
        judge_status(region_score=0.9, extracted_text="甲公司", matched=True, overlap=True)
        == "SUSPICIOUS_OVERLAP"
    )
    assert (
        judge_status(region_score=0.9, extracted_text=None, matched=False, overlap=False)
        == "BLURRY_UNRECOGNIZABLE"
    )
    assert (
        judge_status(region_score=0.9, extracted_text="丙公司", matched=False, overlap=False)
        == "UNAUTHORIZED"
    )
    assert (
        judge_status(region_score=0.9, extracted_text="甲公司", matched=True, overlap=False)
        == "VALID"
    )
    assert (
        judge_status(
            region_score=0.9,
            extracted_text="甲公司",
            matched=False,
            overlap=False,
            parties_provided=False,
        )
        == "VALID"
    )


# ===========================================================================
# 4. 检测器与 payload
# ===========================================================================
def test_detect_without_ocr_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎不可用 → 仍返回区域结果，但文字为空、置信度只按区域分（不抛异常）。"""
    import doc_processor.ocr.engine as engine_module
    from doc_processor.errors import OcrEngineUnavailableError

    def _boom(settings: object = None) -> None:
        raise OcrEngineUnavailableError("rapid", "测试注入")

    monkeypatch.setattr(engine_module, "build_ocr_engine", _boom)
    image = _page()
    _red_circle(image)
    analyses = detect_seals_in_image(image, page=1, src_dpi=200, settings=_settings())
    assert len(analyses) == 1
    analysis = analyses[0]
    assert analysis.detection.extracted_text is None
    assert analysis.status == "BLURRY_UNRECOGNIZABLE"
    assert 0.0 < analysis.detection.confidence <= 0.6  # 只按区域分（0.6 权重）
    assert "未接入 OCR 引擎" in analysis.compliance_note


def test_detect_coordinates_are_pdf_points() -> None:
    """坐标归一：200dpi 下 305 像素 ≈ 109.8 点（不能把像素当点）。"""
    image = _page()
    _red_circle(image)
    analysis = detect_seals_in_image(image, page=1, src_dpi=200, settings=_settings())[0]
    assert analysis.detection.page == 1
    assert 40.0 <= analysis.detection.x <= 60.0
    assert 100.0 <= analysis.detection.width <= 120.0


def test_hints_restrict_then_fall_back(caplog: pytest.LogCaptureFixture) -> None:
    """提示区命中 → 只用提示区；提示区无红色 → 回落全页并记一条 warning。"""
    image = _page()
    _red_circle(image)
    settings = _settings()

    inside = detect_seals_in_image(
        image, page=1, src_dpi=200, settings=settings, hints=[(0.0, 0.0, 595.0, 842.0)]
    )
    assert len(inside) == 1

    with caplog.at_level("WARNING", logger="doc_processor.ocr.seal"):
        fallback = detect_seals_in_image(
            image, page=1, src_dpi=200, settings=settings, hints=[(0.0, 0.0, 10.0, 10.0)]
        )
    assert len(fallback) == 1, "提示区无红色时必须回落全页"
    assert "回落全页扫描" in caplog.text


def test_to_detection_payload_shape() -> None:
    """payload 对齐 openapi `SealSignatureDetection`（含 id 零填充与坐标体系）。"""
    image = _page()
    _red_circle(image)
    analysis = detect_seals_in_image(image, page=3, src_dpi=200, settings=_settings())[0]
    payload = to_detection_payload(analysis, seq=2)
    assert payload["id"] == "seal-003-02"
    assert payload["page"] == 3
    assert payload["bbox"]["coordinateSystem"] == "PDF_POINTS"
    assert set(payload["bbox"]) == {"page", "x", "y", "width", "height", "coordinateSystem"}
    assert set(payload) == {
        "id",
        "type",
        "status",
        "bbox",
        "page",
        "extractedText",
        "isMatchedWithParties",
        "complianceNote",
        "confidence",
    }
    assert 0.0 <= payload["confidence"] <= 1.0


def test_payload_enums_exist_in_openapi() -> None:
    """`type` / `status` 字面量必须能在 SSOT（openapi.yaml）里找到（不定义第二个枚举）。"""
    openapi = (_REPO_ROOT / "packages" / "schema" / "openapi.yaml").read_text(encoding="utf-8")
    for literal in (
        "OFFICIAL_SEAL",
        "FINANCIAL_SEAL",
        "CONTRACT_SEAL",
        "LEGAL_REPRESENTATIVE_SEAL",
        "VALID",
        "SUSPICIOUS_OVERLAP",
        "BLURRY_UNRECOGNIZABLE",
        "UNAUTHORIZED",
    ):
        assert literal in openapi, literal


def test_detect_on_fixture_scan_png() -> None:
    """真实夹具（含红色印章样张）：至少检出 1 枚，且坐标落在 A4 页面内。"""
    import cv2

    image = cv2.imread(str(_FIXTURES / "scan.png"), cv2.IMREAD_COLOR)
    assert image is not None, "夹具缺失：先跑 scripts/make_ocr_fixtures.py"
    analyses = detect_seals_in_image(image, page=1, src_dpi=120, settings=_settings())
    assert analyses, "夹具里有红色圆环，应至少检出一枚"
    box = analyses[0].detection
    assert box.x >= 0 and box.y >= 0


# ===========================================================================
# 5. 红线
# ===========================================================================
def test_no_missing_status() -> None:
    """本模块**不得产出** `MISSING`（"应有未盖"是合同级判断，属模块 2）。

    两条证据：① 源码里没有 `MISSING` 字符串字面量（文档/注释里说明规则不算）；
    ② 穷举 `judge_status` 的所有输入组合，返回集合里不出现 `MISSING`。
    """
    source = (
        Path(__file__).resolve().parents[1] / "doc_processor" / "ocr" / "seal.py"
    ).read_text(encoding="utf-8")
    assert '"MISSING"' not in source and "'MISSING'" not in source

    produced = {
        judge_status(
            region_score=0.5, extracted_text=text, matched=matched, overlap=overlap
        )
        for overlap in (False, True)
        for matched in (False, True)
        for text in (None, "甲公司")
    }
    assert "MISSING" not in produced


def test_no_second_enum() -> None:
    """不得为 `SealType` / `SealStatus` 另立枚举（SSOT 是 openapi.yaml）。"""
    source = (
        Path(__file__).resolve().parents[1] / "doc_processor" / "ocr" / "seal.py"
    ).read_text(encoding="utf-8")
    for banned in ("class SealType", "class SealStatus", "Enum"):
        assert banned not in source


def test_contracts_unchanged() -> None:
    """`SealDetection` 字段冻结（本模块只产出，不改契约）。"""
    import dataclasses as dc

    assert [field.name for field in dc.fields(SealDetection)] == [
        "page",
        "x",
        "y",
        "width",
        "height",
        "extracted_text",
        "confidence",
    ]


def test_detector_settings_are_consumed() -> None:
    """`seal_min_area_ratio` 被真正消费（调大阈值 → 同一张图不再命中）。"""
    image = _page()
    _red_circle(image)
    detector = LocalSealDetector(settings=_settings(seal_min_area_ratio=0.9))
    assert detector.detect(image, page=1, src_dpi=200) == []
