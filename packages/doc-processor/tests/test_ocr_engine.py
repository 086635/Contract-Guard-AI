"""OCR 引擎适配器自检（T-C2 / `3-3 §⑧`）。

覆盖：
1. 引擎探测与**真实引擎出字**（`scan.png` 夹具）；
2. **结构兼容层**（PaddleOCR 2.x / 3.x / RapidOCR 三种返回结构 + 畸形结构）；
3. **DPI 归一**（PDF 渲染 300dpi 与图片 72dpi 两条路径的换算分别正确）；
4. 过滤（低置信度 / 出界框）与**页级超时**（桩引擎 + `timeout_s=1`）；
5. 缺引擎显式抛错（不允许"空引擎"静默产出空结果）。

两条与其它文件重叠的守卫（顶层无引擎 import / 契约未变）按标准 §0.7 只保留一处，
分别见 `test_engine_availability.py` 与 `test_doc_processor_smoke.py`。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from doc_processor.config import OcrSettings
from doc_processor.errors import OcrEngineUnavailableError
from doc_processor.ocr.engine import (
    LocalOcrEngine,
    build_ocr_engine,
    detect_engine,
    render_pdf_page,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

# 一行文本的四点框（左上 / 右上 / 右下 / 左下），像素坐标：300x60
_BOX = [[300.0, 600.0], [600.0, 600.0], [600.0, 660.0], [300.0, 660.0]]
_IMAGE = np.full((1200, 900, 3), 255, dtype=np.uint8)


class _StubEngine:
    """假引擎：直接返回注入的原始 payload（验证兼容层 / 过滤 / 归一，不跑真实推理）。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self, image: Any) -> Any:
        self.calls += 1
        return self.payload


def _settings(**overrides: Any) -> OcrSettings:
    """构造离线配置（`_env_file=None` 避免 .env 干扰）。"""
    base: dict[str, Any] = {
        "engine": "rapid",
        "render_dpi": 300,
        "image_dpi": 72,
        "min_confidence": 0.5,
        "timeout_s": 30,
    }
    base.update(overrides)
    return OcrSettings(_env_file=None, **base)


def _engine_with(payload: Any, **overrides: Any) -> LocalOcrEngine:
    """构造注入了桩引擎的 `LocalOcrEngine`（绕过真实引擎构造）。"""
    engine = LocalOcrEngine(_settings(**overrides))
    engine._impl = _StubEngine(payload)
    return engine


def _payload_c(lines: list[list[Any]]) -> tuple[list[list[Any]], float]:
    """RapidOCR 形状：`([ [box, text, score], ... ], elapse)`。"""
    return lines, 0.012


def _payload_a(lines: list[list[Any]]) -> list[list[list[Any]]]:
    """PaddleOCR 2.x 形状：`[[[box, (text, score)], ...]]`（外层是"页"）。"""
    return [[[box, (text, score)] for box, text, score in lines]]


def _payload_b(lines: list[list[Any]]) -> list[dict[str, Any]]:
    """PaddleOCR 3.x 形状：`[{"rec_texts": ..., "rec_scores": ..., "rec_polys": ...}]`。"""
    return [
        {
            "rec_texts": [text for _, text, _ in lines],
            "rec_scores": [score for _, _, score in lines],
            "rec_polys": [box for box, _, _ in lines],
        }
    ]


# ===========================================================================
# 1. 探测与真实引擎
# ===========================================================================
def test_detect_engine_available() -> None:
    """§⑦ 断言 1：本机至少一个引擎可用（本任务的定义就是"引擎可用"）。"""
    assert detect_engine() in ("rapid", "paddle")


def test_build_ocr_engine_returns_implementation() -> None:
    """工厂返回 `LocalOcrEngine`，且引擎名与探测一致。"""
    engine = build_ocr_engine()
    assert isinstance(engine, LocalOcrEngine)
    assert engine.engine_name == detect_engine()


def test_ocr_page_returns_boxed_lines() -> None:
    """§⑦ 断言 2：真实引擎对 `scan.png` 出字，坐标非负且在页面内。"""
    image_path = _FIXTURES / "scan.png"
    assert image_path.is_file(), f"夹具缺失：{image_path}（先跑 scripts/make_ocr_fixtures.py）"
    results = build_ocr_engine().ocr_page(str(image_path))
    assert results, "真实引擎应至少识别出一行文字"
    assert all(item.text.strip() for item in results)
    assert all(item.x >= 0 and item.y >= 0 and item.confidence > 0 for item in results)
    # 阅读顺序稳定（按 y 再按 x）
    keys = [(round(item.y, 1), item.x) for item in results]
    assert keys == sorted(keys)


def test_render_pdf_page_returns_bgr_image() -> None:
    """渲染入口返回 BGR 图像与像素尺寸（0-based 页索引）。"""
    pdf = _FIXTURES / "native.pdf"
    image, width, height = render_pdf_page(str(pdf), 0, dpi=150)
    assert image.ndim == 3 and image.shape[2] == 3
    assert (width, height) == (image.shape[1], image.shape[0])
    assert width > 0 and height > 0


def test_ocr_pdf_page_uses_render_dpi() -> None:
    """PDF 页路径的出字坐标应落在 A4 页面范围内（render_dpi=200）。"""
    engine = LocalOcrEngine(_settings(render_dpi=200))
    results = engine.ocr_pdf_page(str(_FIXTURES / "native.pdf"), 0)
    assert results, "数字版 PDF 渲染后也应能 OCR 出字"
    assert all(item.page == 1 for item in results)
    assert all(0 <= item.x <= 595 and 0 <= item.y <= 900 for item in results)


# ===========================================================================
# 2. 结构兼容层
# ===========================================================================
@pytest.mark.parametrize("shape", ["A_paddle2", "B_paddle3", "C_rapid"])
def test_iter_lines_handles_three_shapes(shape: str) -> None:
    """§⑦ 断言 4：三种引擎返回结构都解析成 `(box, text, score)`。"""
    lines = [[_BOX, "逾期付款违约金", 0.97], [[[1.0, 2.0], [11.0, 2.0], [11.0, 12.0], [1.0, 12.0]], "第二条", 0.91]]
    payload = {"A_paddle2": _payload_a, "B_paddle3": _payload_b, "C_rapid": _payload_c}[shape](lines)
    parsed = list(LocalOcrEngine._iter_lines(payload))
    assert [text for _, text, _ in parsed] == ["逾期付款违约金", "第二条"]
    assert [round(score, 2) for _, _, score in parsed] == [0.97, 0.91]


@pytest.mark.parametrize("payload", [None, [], {}, {"foo": "bar"}, 42, ("bad", 1.0)])
def test_iter_lines_malformed_is_empty(payload: Any) -> None:
    """畸形 payload → 空结果且**不抛异常**（某一版引擎改结构不能打挂链路）。"""
    assert list(LocalOcrEngine._iter_lines(payload)) == []


def test_parse_line_entry_rejects_unknown_shape() -> None:
    """行条目形状不符（既不是 A 也不是 C）→ 返回 None，由上层跳过。"""
    assert LocalOcrEngine._parse_line_entry([_BOX, 1, 2, 3]) is None
    assert LocalOcrEngine._parse_line_entry([_BOX]) is None


# ===========================================================================
# 3. DPI 归一（本模块最致命的坑）
# ===========================================================================
def test_dpi_normalization_render_path() -> None:
    """渲染路径：`src_dpi=300` 时 300x60 像素 → 72x14.4 点（3-3 §⑦ 断言 3 的数字）。"""
    engine = _engine_with(_payload_c([[_BOX, "文本", 0.99]]))
    result = engine.ocr_image(_IMAGE, page=1, src_dpi=300)[0]
    # 注意用 approx：60 像素 × (72/300) = 14.399999999999999，精确比较会假失败
    assert (result.x, result.y, result.width, result.height) == pytest.approx(
        (72.0, 144.0, 72.0, 14.4)
    )


def test_dpi_normalization_image_path() -> None:
    """图片路径：`image_dpi=72`（1 像素 = 1 点）→ 坐标原样保留。"""
    engine = _engine_with(_payload_c([[_BOX, "文本", 0.99]]))
    result = engine.ocr_image(_IMAGE, page=1, src_dpi=72)[0]
    assert (result.x, result.y, result.width, result.height) == pytest.approx(
        (300.0, 600.0, 300.0, 60.0)
    )


def test_no_handwritten_scaling_in_engine() -> None:
    """源码级守卫：不得手写 `* 72 / dpi` 之类的缩放（唯一处是 `to_pdf_points()`）。"""
    source = (Path(__file__).resolve().parents[1] / "doc_processor" / "ocr" / "engine.py").read_text(
        encoding="utf-8"
    )
    assert "72 / src_dpi" not in source
    assert "72.0 / src_dpi" not in source
    assert "to_pdf_points" in source


# ===========================================================================
# 4. 过滤与超时
# ===========================================================================
def test_low_confidence_filtered() -> None:
    """低于 `min_confidence` 的行被丢弃。"""
    engine = _engine_with(_payload_c([[_BOX, "低置信", 0.10], [_BOX, "高置信", 0.90]]))
    texts = [item.text for item in engine.ocr_image(_IMAGE, page=1, src_dpi=72)]
    assert texts == ["高置信"]


def test_out_of_page_box_filtered() -> None:
    """出界框（超出页面 + 容差）被丢弃，页面内的保留。"""
    outside = [[5000.0, 5000.0], [5100.0, 5000.0], [5100.0, 5060.0], [5000.0, 5060.0]]
    engine = _engine_with(_payload_c([[outside, "出界", 0.99], [_BOX, "页内", 0.99]]))
    texts = [item.text for item in engine.ocr_image(_IMAGE, page=1, src_dpi=72)]
    assert texts == ["页内"]


def test_timeout_marks_page_skipped(caplog: pytest.LogCaptureFixture) -> None:
    """超时 → 该页返回 `[]` 且记录 `ocr.page.timeout`（不抛异常）。"""

    class _SlowEngine:
        def __call__(self, image: Any) -> Any:
            time.sleep(1.5)
            return _payload_c([[_BOX, "太慢了", 0.99]])

    engine = LocalOcrEngine(_settings(timeout_s=1))
    engine._impl = _SlowEngine()
    with caplog.at_level("INFO", logger="doc_processor.ocr.engine"):
        started = time.perf_counter()
        assert engine.ocr_image(_IMAGE, page=1, src_dpi=72) == []
        elapsed = time.perf_counter() - started
    assert elapsed < 1.4, f"超时未生效（耗时 {elapsed:.2f}s，等于在等它跑完）"
    assert "ocr.page.timeout" in caplog.text


# ===========================================================================
# 5. 缺引擎必须显式失败
# ===========================================================================
def test_engine_unavailable_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§⑦ 断言 5：探测为 `none` 时工厂抛 `OcrEngineUnavailableError`（不返回空引擎）。"""
    import doc_processor.ocr.engine as engine_module

    monkeypatch.setattr(engine_module, "detect_engine", lambda settings=None: "none")
    with pytest.raises(OcrEngineUnavailableError):
        build_ocr_engine()


def test_ocr_image_raises_when_engine_missing() -> None:
    """引擎缺失时 `ocr_image()` 也显式抛错，而不是返回空列表。"""
    engine = LocalOcrEngine(_settings())
    engine._engine_name = "none"
    with pytest.raises(OcrEngineUnavailableError):
        engine.ocr_image(_IMAGE, page=1, src_dpi=72)


def test_seal_detection_delegates_to_seal_module() -> None:
    """`seal.py`（T-C6）落盘后：`detect_seals()` 确实委托过去，能返回真实检测结果。"""
    engine = LocalOcrEngine(_settings())
    detections = engine.detect_seals(str(_FIXTURES / "scan.png"))
    assert detections, "夹具含红色印章样张，委托链路应返回至少一枚"
    assert all(item.page >= 1 and item.width > 0 for item in detections)


def test_seal_detection_degrades_when_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """`3-3 §⑥-9`：`seal.py` **不可导入**时返回 `[]` 且不抛异常。

    注：模块落盘后不再用"文件不存在"来构造该场景，改为把 `sys.modules` 里的条目置 None
    （效果与未落盘一致：import 抛 `ImportError`），这样降级契约仍然被守住。
    """
    import sys

    engine = LocalOcrEngine(_settings())
    monkeypatch.setitem(sys.modules, "doc_processor.ocr.seal", None)
    assert engine.detect_seals(str(_FIXTURES / "scan.png")) == []
