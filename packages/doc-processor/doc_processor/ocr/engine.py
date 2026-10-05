"""OCR 引擎适配器（T-C2 / `3-3`）。

三条硬约束（写代码前先记住）：
1. **延迟导入**：引擎的 `import` 只允许出现在**函数体内** —— 缺引擎时整包仍要能导入（否则现状 F4 会连锁崩）。
2. **DPI 归一**：任何像素坐标出模块前必须经 `BBoxAligner.to_pdf_points(..., src_dpi=...)`；
   PDF 页用 `render_dpi`、独立图片用 `image_dpi`。**丢了 DPI 不会报错，只会在演示时肉眼发现错位**。
3. **缺引擎要显式抛错**：不允许返回"空引擎"让下游静默产出空结果。
"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

import numpy as np

from cg_observability import ensure_trace_id, get_logger, log_event

from ..aligner import BBoxAligner
from ..config import OcrSettings, get_ocr_settings
from ..errors import OcrEngineUnavailableError, RenderFailedError
from . import OcrEngine, OcrResult, SealDetection

_LOG = get_logger("doc_processor.ocr.engine")

# 出界容差（点）：引擎在边缘上常给出 1~2pt 的越界框，属正常抖动而非错误
_OUT_OF_PAGE_TOLERANCE_PT = 2.0

_SEAL_IMPORT_WARNED = False


def _module_available(name: str) -> bool:
    """模块是否可被解析（不真正导入，避免触发重型依赖副作用）。

    Args:
        name: 模块名。

    Returns:
        可解析返回 True。
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # 父包缺失 / 模块名非法
        return False


def detect_engine(settings: OcrSettings | None = None) -> str:
    """探测可用引擎（**不抛异常**）。

    顺序：`auto` → 先 RapidOCR 后 PaddleOCR；显式指定则只认指定引擎。

    Args:
        settings: 配置；为空取单例。

    Returns:
        `"rapid"` / `"paddle"` / `"none"`。
    """
    cfg = settings or get_ocr_settings()
    order: tuple[str, ...]
    if cfg.engine == "rapid":
        order = ("rapid",)
    elif cfg.engine == "paddle":
        order = ("paddle",)
    else:
        order = ("rapid", "paddle")

    for name in order:
        if name == "rapid" and _module_available("rapidocr_onnxruntime"):
            return "rapid"
        if (
            name == "paddle"
            and _module_available("paddleocr")
            and _module_available("paddle")
        ):
            return "paddle"
    return "none"


def render_pdf_page(
    pdf_path: str, page_index: int, *, dpi: int
) -> tuple[np.ndarray, int, int]:
    """把 PDF 指定页渲染为 BGR 图像（**唯一渲染入口**）。

    为什么必须只有一处：渲染 DPI 决定像素 → 点的换算系数，任何"另一条渲染路径"
    都会让坐标体系出现第二个真相（模块总纲 §3.4.2）。

    Args:
        pdf_path: PDF 路径。
        page_index: 页索引（**0-based**，与 pypdfium2 一致）。
        dpi: 渲染 DPI。

    Returns:
        `(image, width_px, height_px)`；`image` 为 `numpy` **BGR**（与 opencv 对齐）。

    Raises:
        RenderFailedError: 文件无法打开，或页索引越界。
    """
    import pypdfium2 as pdfium  # 延迟导入：默认路径（数字版 PDF 直抽）不需要它

    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:  # 统一转领域异常（异常链已用 from 保留）
        raise RenderFailedError(pdf_path, f"PDF 无法打开：{exc!r}") from exc

    try:
        page_count = len(document)
        if page_index < 0 or page_index >= page_count:
            raise RenderFailedError(
                pdf_path, f"页索引越界：{page_index}（共 {page_count} 页，0-based）"
            )
        bitmap = document[page_index].render(scale=dpi / 72.0)
        rgb = bitmap.to_numpy()
        # to_numpy() 可能是位图缓冲的视图，必须先拷贝再释放文档
        image = np.ascontiguousarray(rgb[:, :, ::-1].copy())  # RGB → BGR
    finally:
        document.close()

    return image, int(image.shape[1]), int(image.shape[0])


class LocalOcrEngine(OcrEngine):
    """本机 OCR 引擎实现（延迟导入 + 多引擎结构兼容）。

    Attributes:
        settings: 配置。
        engine_name: 实际生效的引擎名（`rapid` / `paddle` / `none`）。
    """

    def __init__(self, settings: OcrSettings | None = None) -> None:
        """初始化（**不构造底层引擎**，首次调用时惰性构造并缓存）。

        Args:
            settings: 配置；为空取单例。
        """
        self._settings: OcrSettings = settings or get_ocr_settings()
        self._engine_name: str = detect_engine(self._settings)
        self._impl: Any = None

    @property
    def settings(self) -> OcrSettings:
        """配置。"""
        return self._settings

    @property
    def engine_name(self) -> str:
        """返回实际生效的引擎名（`rapid` / `paddle` / `none`）。"""
        return self._engine_name

    # ---------------- 对外：图片 / PDF 页 ----------------
    def ocr_page(self, image_path: str) -> list[OcrResult]:
        """对单张图片做 OCR（图片按 `settings.image_dpi` 解释）。

        Args:
            image_path: 图片路径。

        Returns:
            行级 OCR 结果（坐标已归一为 PDF_POINTS）。

        Raises:
            RenderFailedError: 图像不可读。
        """
        image = self._read_image(Path(image_path))
        return self.ocr_image(image, page=1, src_dpi=self._settings.image_dpi)

    def ocr_pdf_page(self, pdf_path: str, page_index: int) -> list[OcrResult]:
        """渲染 PDF 指定页（`render_dpi`）后 OCR。

        Args:
            pdf_path: PDF 路径。
            page_index: 页索引（0-based）。

        Returns:
            行级 OCR 结果（`page` 为 1-based）。

        Raises:
            RenderFailedError: 渲染失败。
        """
        image, _, _ = render_pdf_page(
            pdf_path, page_index, dpi=self._settings.render_dpi
        )
        return self.ocr_image(
            image, page=page_index + 1, src_dpi=self._settings.render_dpi
        )

    def ocr_image(
        self, image: np.ndarray, *, page: int, src_dpi: int
    ) -> list[OcrResult]:
        """对内存图像做 OCR（**工作马**：图片与 PDF 渲染路径都走它）。

        Args:
            image: BGR 图像（`numpy`）。
            page: 页码（1-based）。
            src_dpi: 图像的 DPI（PDF 渲染用 `render_dpi`；独立图片用 `image_dpi`）。

        Returns:
            行级 OCR 结果；引擎不可用或单页超时时返回 `[]`（超时路径已记 warning）。

        Raises:
            OcrEngineUnavailableError: 无可用引擎。
        """
        if self._engine_name == "none":
            raise OcrEngineUnavailableError(self._settings.engine, "未安装任何可用引擎")

        started = time.perf_counter()
        payload = self._invoke_with_timeout(image, page=page)
        if payload is None:  # 超时：该页结果整体丢弃
            return []

        height, width = int(image.shape[0]), int(image.shape[1])
        tolerance = _OUT_OF_PAGE_TOLERANCE_PT * src_dpi / 72.0
        aligner = BBoxAligner()
        results: list[OcrResult] = []
        filtered_low_conf = 0
        filtered_out_of_page = 0
        malformed = 0

        for box, text, score in self._iter_lines(payload):
            rect = self._as_rect(box)
            if rect is None or not text:
                malformed += 1
                continue
            if score < self._settings.min_confidence:
                filtered_low_conf += 1
                continue
            x, y, box_w, box_h = rect
            if (
                x < -tolerance
                or y < -tolerance
                or x + box_w > width + tolerance
                or y + box_h > height + tolerance
            ):
                filtered_out_of_page += 1
                continue
            normalized = aligner.to_pdf_points(
                page, x, y, box_w, box_h, src_dpi=src_dpi
            )
            results.append(
                OcrResult(
                    page=normalized.page,
                    x=normalized.x,
                    y=normalized.y,
                    width=normalized.width,
                    height=normalized.height,
                    text=text,
                    confidence=round(float(score), 4),
                )
            )

        # 阅读顺序稳定：同一页按 y 再按 x（下游 3-4 依赖行序切条款）
        results.sort(key=lambda item: (item.page, round(item.y, 1), item.x))
        log_event(
            _LOG,
            "ocr.page",
            trace_id=ensure_trace_id(),
            page=page,
            engine=self._engine_name,
            lines=len(results),
            filtered_low_conf=filtered_low_conf,
            filtered_out_of_page=filtered_out_of_page,
            malformed=malformed,
            src_dpi=src_dpi,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return results

    def detect_seals(self, image_path: str) -> list[SealDetection]:
        """印章检测（委托 `seal.py`；**3-8 未落盘时返回 `[]` + 一条 warning**）。

        Args:
            image_path: 图片路径。

        Returns:
            `SealDetection` 列表；印章模块缺失时为空列表（不抛异常）。
        """
        global _SEAL_IMPORT_WARNED
        try:
            from .seal import detect_seals_in_image  # 3-8（T-C6）落盘后才有
        except ImportError:
            if not _SEAL_IMPORT_WARNED:
                _SEAL_IMPORT_WARNED = True
                _LOG.warning("seal.py 未落盘（T-C6），印章检测暂时返回空结果")
            return []
        image = self._read_image(Path(image_path))
        analyses = detect_seals_in_image(
            image, page=1, src_dpi=self._settings.image_dpi, settings=self._settings
        )
        return [analysis.detection for analysis in analyses]

    # ---------------- 结构兼容层（唯一处） ----------------
    @classmethod
    def _iter_lines(
        cls, payload: Any
    ) -> Iterator[tuple[Sequence[Sequence[float]], str, float]]:
        """把三种引擎返回结构统一成 `(四点框, 文本, 置信度)`。

        支持（`3-3 §⑤` 的结构兼容层）：
          A · PaddleOCR 2.x：`[[[box, (text, score)], ...]]`  ← 外层是"页"
          B · PaddleOCR 3.x：`[{"rec_texts": [...], "rec_scores": [...], "rec_polys"|"dt_polys": [...]}]`
          C · RapidOCR     ：`([ [box, text, score], ... ], elapse)`

        任何无法识别的形状 → 记 warning 并跳过，**不抛异常**
        （防某一版引擎改结构把整条链路打挂）。

        Args:
            payload: 引擎原始返回。

        Yields:
            `(四点框, 文本, 置信度)`。
        """
        if payload is None:
            return
        if isinstance(payload, tuple):  # C：丢掉第二项耗时
            payload = payload[0] if payload else []
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            yield from cls._iter_paddle_v3(payload[0])  # B
            return
        if (
            isinstance(payload, list)
            and len(payload) == 1
            and isinstance(payload[0], list)
            and payload[0]
            and isinstance(payload[0][0], (list, tuple))
            and len(payload[0][0]) == 2
            and isinstance(payload[0][0][1], (list, tuple))
        ):
            payload = payload[0]  # A：解掉外层"页"
        if not isinstance(payload, list):
            _LOG.warning(
                "OCR 返回结构无法识别（type=%s），已跳过", type(payload).__name__
            )
            return
        for entry in payload:
            parsed = cls._parse_line_entry(entry)
            if parsed is None:
                _LOG.warning(
                    "OCR 行条目无法识别（type=%s），已跳过", type(entry).__name__
                )
                continue
            yield parsed

    @staticmethod
    def _iter_paddle_v3(
        block: dict
    ) -> Iterator[tuple[Sequence[Sequence[float]], str, float]]:
        """解析 PaddleOCR 3.x 的 dict 结构（并行数组）。

        Args:
            block: 形如 `{"rec_texts": [...], "rec_scores": [...], "rec_polys": [...]}`。

        Yields:
            `(四点框, 文本, 置信度)`。
        """
        texts = block.get("rec_texts") or []
        scores = block.get("rec_scores") or []
        polys = block.get("rec_polys") or block.get("dt_polys") or []
        for box, text, score in zip(polys, texts, scores):
            yield box, str(text), float(score)

    @staticmethod
    def _parse_line_entry(
        entry: Any,
    ) -> tuple[Sequence[Sequence[float]], str, float] | None:
        """解析单行条目：C 形 `[box, text, score]` 或 A 形 `[box, (text, score)]`。

        Args:
            entry: 引擎返回的一行。

        Returns:
            `(四点框, 文本, 置信度)`；形状不符返回 None。
        """
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            return None
        box, rest = entry[0], entry[1]
        if isinstance(rest, str):  # C · RapidOCR
            if len(entry) < 3:
                return None
            try:
                return box, rest, float(entry[2])
            except (TypeError, ValueError):
                return None
        if isinstance(rest, (list, tuple)) and len(rest) == 2:  # A · PaddleOCR 2.x
            try:
                return box, str(rest[0]), float(rest[1])
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _as_rect(
        box: Sequence[Sequence[float]],
    ) -> tuple[float, float, float, float] | None:
        """四点框 → 外接矩形 `(x, y, w, h)`；非法返回 None。

        Args:
            box: 四点坐标（左上 / 右上 / 右下 / 左下）。

        Returns:
            外接矩形；点数不足或非数值返回 None。
        """
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, IndexError, ValueError):
            return None
        if len(xs) < 3:
            return None
        x0, y0 = min(xs), min(ys)
        return x0, y0, max(xs) - x0, max(ys) - y0

    # ---------------- 内部：引擎生命周期 ----------------
    def _ensure_impl(self) -> Any:
        """惰性构造并缓存底层引擎（构造开销数秒级，**绝不允许每页重建**）。

        Returns:
            底层引擎实例。

        Raises:
            OcrEngineUnavailableError: 无可用引擎。
        """
        if self._impl is not None:
            return self._impl
        name = self._engine_name
        if name == "none":
            raise OcrEngineUnavailableError(self._settings.engine, "未安装任何可用引擎")
        if name == "rapid":
            from rapidocr_onnxruntime import RapidOCR  # 延迟导入（函数内）

            self._impl = RapidOCR()
            return self._impl

        from paddleocr import PaddleOCR  # 延迟导入（函数内）

        kwargs: dict[str, Any] = {"lang": self._settings.lang}
        if self._settings.model_dir:
            # ⚠️ 2.x 的参数名；3.x 改名为 xxx_model_dir 系列（本机走 RapidOCR，未验证 3.x）
            kwargs["det_model_dir"] = self._settings.model_dir
            kwargs["rec_model_dir"] = self._settings.model_dir
        self._impl = PaddleOCR(**kwargs)
        return self._impl

    def _invoke_with_timeout(self, image: np.ndarray, *, page: int) -> Any | None:
        """在线程里调用引擎，按 `timeout_s` 做**页级超时**。

        ⚠️ **已知限制**：Python 无法强杀线程，被超时的引擎调用仍会在后台跑完（占 CPU）。
        本实现只保证"该页结果被丢弃 + 记 warning"，**不得**对外声称"已中断引擎"。

        Args:
            image: BGR 图像。
            page: 页码（仅用于日志）。

        Returns:
            引擎原始返回；超时返回 None。
        """
        impl = self._ensure_impl()
        name = self._engine_name

        def _call() -> Any:
            if name == "rapid":
                return impl(image)
            if hasattr(impl, "ocr"):  # PaddleOCR 2.x
                return impl.ocr(image, cls=True)
            return impl.predict(image)  # PaddleOCR 3.x

        # ⚠️ 刻意**不用** `with ThreadPoolExecutor(...)`：它的 __exit__ 会 wait=True，
        # 那会让"超时"退化回"等它慢慢跑完"（超时形同虚设）。
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_call)
        try:
            payload = future.result(timeout=self._settings.timeout_s)
        except FuturesTimeoutError:
            pool.shutdown(wait=False)  # 不等待：线程无法强杀，已知限制见 docstring
            log_event(
                _LOG,
                "ocr.page.timeout",
                level="WARNING",
                trace_id=ensure_trace_id(),
                page=page,
                engine=name,
                timeout_s=self._settings.timeout_s,
                note="线程无法强杀，引擎调用会在后台跑完",
            )
            return None
        pool.shutdown(wait=False)
        return payload

    @staticmethod
    def _read_image(path: Path) -> np.ndarray:
        """读图为 BGR `numpy` 数组。

        Args:
            path: 图片路径。

        Returns:
            BGR 图像。

        Raises:
            RenderFailedError: 图像不可读。
        """
        import cv2  # 延迟导入（与引擎同源，避免默认路径拖入 opencv）

        # OpenCV 的 Windows 路径入口可能无法打开中文目录；先由 Python 读取再解码。
        try:
            encoded = np.frombuffer(path.read_bytes(), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        except (OSError, ValueError, cv2.error) as exc:
            raise RenderFailedError(path, "图像无法读取（格式不支持或文件损坏）") from exc
        if image is None:
            raise RenderFailedError(path, "图像无法读取（格式不支持或文件损坏）")
        return image


def build_ocr_engine(settings: OcrSettings | None = None) -> OcrEngine:
    """按配置构造引擎。

    Args:
        settings: 配置；为空取单例。

    Returns:
        `OcrEngine` 实现。

    Raises:
        OcrEngineUnavailableError: 探测结果为 `"none"`（**显式失败，不返回空引擎**）。
    """
    cfg = settings or get_ocr_settings()
    name = detect_engine(cfg)
    if name == "none":
        raise OcrEngineUnavailableError(cfg.engine, "detect_engine() 未发现可用引擎")
    return LocalOcrEngine(cfg)


__all__ = [
    "LocalOcrEngine",
    "build_ocr_engine",
    "detect_engine",
    "render_pdf_page",
]
