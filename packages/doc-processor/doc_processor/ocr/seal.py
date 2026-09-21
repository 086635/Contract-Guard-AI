"""印章三段式检测（T-C6 / `3-8`）：**全程不需要深度学习模型**。

为什么自己实现：`SealDetection` 要求"定位印章 + 识别印章内文字（公司名 / 税号）"，
而通用 OCR 对**红色圆形印章**的识别率极低（文字沿圆弧排列 + 红色背景 + 常被签名遮挡），
PP-Structure 之类只能给"印章区域"、不给文字。三段式（颜色分割 → 极坐标展开 → 模糊比对）
是全项目的**创新点**，也是唯一能落地在"CPU 可跑、低配部署"约束下的方案。

三段式：
  ① 区域检测：HSV **双段**红色掩膜（红色跨 0/180 两端，只写一段会漏掉一半红章）
     → 形态学闭运算（印章常被签名/正文遮挡，笔画会断）→ 轮廓筛选（面积 / 长宽比 / 圆度）
  ② 文字识别：极坐标展开把圆环文字拉成**水平文本条**，再送通用 OCR（四方向取最高置信度）
  ③ 主体比对：`difflib` 与甲乙方名称模糊比对（零依赖）
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..config import OcrSettings, get_ocr_settings
from . import OcrEngine, SealDetection

# ---- 常量（**唯一处**：见 `3-8 §5.3`）----
HSV_RED_LOW = ((0, 70, 50), (10, 255, 255))      # 红色下段
HSV_RED_HIGH = ((170, 70, 50), (180, 255, 255))  # 红色上段（跨 0/180，必须并集）
MORPH_KERNEL_SIZE = 15        # 闭运算核（连接被签名/正文遮挡的笔画）
ASPECT_RANGE = (0.6, 1.7)     # 外接矩形长宽比可接受区间（印章近似方形）
MIN_CIRCULARITY = 0.35        # 圆度下限 4πA / P²
PADDING_RATIO = 0.10          # 候选框外扩比例（避免裁掉边缘笔画）
PARTY_MATCH_THRESHOLD = 0.60  # 主体模糊匹配阈值
REGION_SCORE_WEIGHT = 0.6     # confidence 合成权重（区域）
OCR_SCORE_WEIGHT = 0.4        # confidence 合成权重（文字）
MAX_SEALS_PER_PAGE = 8        # 单页候选上限（防红色商标/表头把候选刷爆）

#: 文字条尝试的旋转角度（环形文字展开后可能是横向或倒向，只试 0° 一定漏）
_ROTATIONS = (None, "cw90", "rot180", "ccw90")

# 引擎不可用只告警一次（逐页刷屏会淹掉真正的错误）
_ENGINE_WARNED = False


@dataclass(frozen=True, slots=True)
class SealAnalysis:
    """印章判定结果（本模块内部载体，不进契约层）。

    Attributes:
        detection: 冻结契约 `SealDetection`（几何 + 文本 + 置信度）。
        seal_type: ∈ openapi `SealType` 字面量。
        status: ∈ openapi `SealStatus` 字面量（**不含 `MISSING`**）。
        is_matched_with_parties: 印章文字是否与甲乙方名称匹配。
        party_ratio: 与主体名的最高相似度（0~1）。
        compliance_note: 中文一句话（进报告）。
    """

    detection: SealDetection
    seal_type: str
    status: str
    is_matched_with_parties: bool
    party_ratio: float
    compliance_note: str


def red_mask(image: np.ndarray) -> np.ndarray:
    """红色掩膜（**双段并集，唯一处**）。

    Args:
        image: BGR 图像。

    Returns:
        单通道 `uint8` 掩膜（0 / 255）。
    """
    import cv2

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    low = cv2.inRange(hsv, np.array(HSV_RED_LOW[0], np.uint8), np.array(HSV_RED_LOW[1], np.uint8))
    high = cv2.inRange(hsv, np.array(HSV_RED_HIGH[0], np.uint8), np.array(HSV_RED_HIGH[1], np.uint8))
    return cv2.bitwise_or(low, high)


def _candidates(
    image: np.ndarray, *, min_area_ratio: float
) -> list[tuple[tuple[int, int, int, int], float]]:
    """红色候选框（像素坐标）+ 圆度，按面积降序，已外扩、已截断。

    Args:
        image: BGR 图像。
        min_area_ratio: 最小面积占比（相对整页）。

    Returns:
        `[((x, y, w, h), circularity), ...]`。
    """
    import cv2

    height, width = int(image.shape[0]), int(image.shape[1])
    mask = red_mask(image)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (MORPH_KERNEL_SIZE, MORPH_KERNEL_SIZE)
    )
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    page_area = float(height * width) or 1.0
    found: list[tuple[tuple[int, int, int, int], float]] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < page_area * min_area_ratio:
            continue
        x, y, box_width, box_height = cv2.boundingRect(contour)
        if box_height <= 0 or box_width <= 0:
            continue
        aspect = box_width / box_height
        if not (ASPECT_RANGE[0] <= aspect <= ASPECT_RANGE[1]):
            continue
        perimeter = float(cv2.arcLength(contour, True))
        circularity = (4 * np.pi * area / (perimeter**2)) if perimeter > 0 else 0.0
        if circularity < MIN_CIRCULARITY:
            continue
        pad_x = int(box_width * PADDING_RATIO)
        pad_y = int(box_height * PADDING_RATIO)
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(width, x + box_width + pad_x)
        y1 = min(height, y + box_height + pad_y)
        found.append(((x0, y0, x1 - x0, y1 - y0), float(circularity)))

    found.sort(key=lambda item: item[0][2] * item[0][3], reverse=True)
    return found[:MAX_SEALS_PER_PAGE]


def find_seal_regions(
    image: np.ndarray, *, min_area_ratio: float
) -> list[tuple[int, int, int, int]]:
    """红色候选区域（像素坐标 `(x, y, w, h)`，已外扩）。

    流程：`red_mask` → 闭运算 → 轮廓 → 面积 ≥ 页面面积 × `min_area_ratio`
    → 长宽比 ∈ `ASPECT_RANGE` → 圆度 ≥ `MIN_CIRCULARITY` → 按面积降序取前 `MAX_SEALS_PER_PAGE`。

    Args:
        image: BGR 图像。
        min_area_ratio: 最小面积占比。

    Returns:
        候选框列表（像素坐标）。
    """
    return [box for box, _ in _candidates(image, min_area_ratio=min_area_ratio)]


def expand_ring(
    image: np.ndarray, box: tuple[int, int, int, int], *, steps: int
) -> np.ndarray:
    """把候选框内的圆环展开为水平文本条（**唯一处**）。

    实现固定为 `cv2.warpPolar(..., dsize=(半径, steps), WARP_POLAR_LINEAR)`：
    输出形状 `(steps, 半径, 3)`，**行 = 角度、列 = 半径** —— 环形文字因此变成横向文本条。

    Args:
        image: BGR 图像。
        box: 候选框 `(x, y, w, h)`（像素）。
        steps: 角度采样数（`OcrSettings.seal_ring_steps`）。

    Returns:
        BGR 图像；候选框退化（宽或高为 0）时返回空数组。
    """
    import cv2

    x, y, width, height = box
    if width <= 0 or height <= 0:
        return np.empty((0, 0, 3), dtype=np.uint8)
    side = max(int(width), int(height))
    center = (float(x) + width / 2.0, float(y) + height / 2.0)
    radius = side / 2.0
    return cv2.warpPolar(
        image, (int(radius), int(steps)), center, radius, cv2.WARP_POLAR_LINEAR
    )


def _normalize(text: str) -> str:
    """归一化文本：全角 → 半角、去空白与中英文标点（**保留"有限公司"等后缀**）。"""
    normalized = unicodedata.normalize("NFKC", text)
    drop = set(" \t\r\n:：,，.。;；、()（）[]【】<>《》-—_/\\|!！?？'\"")
    return "".join(ch for ch in normalized if ch not in drop)


def match_parties(text: str | None, party_names: Any) -> tuple[bool, float]:
    """印章文字与甲乙方名称的模糊比对（**唯一处**）。

    Args:
        text: 印章 OCR 文字（可为 None）。
        party_names: 甲乙方名称序列。

    Returns:
        `(是否命中, 最高相似度)`；`text` 为空或 `party_names` 为空 → `(False, 0.0)`。
    """
    if not text or not party_names:
        return False, 0.0
    normalized_text = _normalize(text)
    if not normalized_text:
        return False, 0.0
    best = 0.0
    matched = False
    for name in party_names:
        normalized_name = _normalize(str(name))
        if not normalized_name:
            continue
        if normalized_name in normalized_text:
            return True, 1.0
        ratio = difflib.SequenceMatcher(None, normalized_text, normalized_name).ratio()
        best = max(best, ratio)
        if ratio >= PARTY_MATCH_THRESHOLD:
            matched = True
    return matched, round(best, 4)


def classify_seal_type(text: str | None) -> str:
    """按关键词判印章类型（规则表见 `3-8 §5.4`，**唯一处**）。

    Args:
        text: 印章文字。

    Returns:
        openapi `SealType` 字面量（默认 `OFFICIAL_SEAL`）。
    """
    normalized = _normalize(text or "")
    if "财务" in normalized:
        return "FINANCIAL_SEAL"
    if "合同" in normalized:
        return "CONTRACT_SEAL"
    if "法定代表人" in normalized or "法人" in normalized:
        return "LEGAL_REPRESENTATIVE_SEAL"
    return "OFFICIAL_SEAL"


def judge_status(
    *,
    region_score: float,  # 保留形参：状态判定表未来可能用到区域分
    extracted_text: str | None,
    matched: bool,
    overlap: bool,
    parties_provided: bool = True,
) -> str:
    """判定印章状态（规则表见 `3-8 §5.4`，**唯一处**；不产出 `MISSING`）。

    Args:
        region_score: 区域置信分（0~1）。
        extracted_text: 印章文字（None = 未识别）。
        matched: 是否与甲乙方名称匹配。
        overlap: 是否与另一候选框重叠（IoU ≥ 0.5）。
        parties_provided: 是否提供了甲乙方名称。**未提供时不做"未授权"判定**
            （没比对过就不能判它不匹配）—— 此时 `matched=False` 只是"没比对"的缺省值。

    Returns:
        openapi `SealStatus` 字面量。
    """
    if overlap:
        return "SUSPICIOUS_OVERLAP"
    if not extracted_text:
        return "BLURRY_UNRECOGNIZABLE"
    if parties_provided and not matched:
        return "UNAUTHORIZED"
    return "VALID"


#: 旋转操作名（`cv2` 常量名 → 便于表驱动）
_ROTATION_OPS: dict[str | None, str | None] = {
    None: None,
    "cw90": "ROTATE_90_CLOCKWISE",
    "rot180": "ROTATE_180",
    "ccw90": "ROTATE_90_COUNTERCLOCKWISE",
}


def _iou(left: dict, right: dict) -> float:
    """两个 openapi `BBox` 的 IoU（同页才有意义）。"""
    if left["page"] != right["page"]:
        return 0.0
    x0 = max(left["x"], right["x"])
    y0 = max(left["y"], right["y"])
    x1 = min(left["x"] + left["width"], right["x"] + right["width"])
    y1 = min(left["y"] + left["height"], right["y"] + right["height"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    intersection = (x1 - x0) * (y1 - y0)
    union = left["width"] * left["height"] + right["width"] * right["height"] - intersection
    return intersection / union if union > 0 else 0.0


class LocalSealDetector:
    """三段式印章检测器（区域 → 展开 → 比对）。

    Attributes:
        settings: 配置。
    """

    def __init__(self, settings: OcrSettings | None = None, ocr: OcrEngine | None = None) -> None:
        """初始化。

        Args:
            settings: 配置；为空取单例。
            ocr: OCR 引擎；为空时**惰性构造**（构造失败按"引擎不可用"降级）。
        """
        self._settings: OcrSettings = settings or get_ocr_settings()
        self._ocr = ocr

    @property
    def settings(self) -> OcrSettings:
        """配置。"""
        return self._settings

    def detect(
        self,
        image: np.ndarray,
        *,
        page: int,
        src_dpi: int,
        party_names: Any = (),
        hints: Any = (),
    ) -> list[SealAnalysis]:
        """对单页图像做印章检测。

        Args:
            image: BGR 图像。
            page: 页码（1-based）。
            src_dpi: 图像 DPI（坐标归一**必须**用它）。
            party_names: 甲乙方名称（空 → 不做主体比对）。
            hints: 区域提示框（**PDF_POINTS**，来自 3-7 的 `SEAL` 块）：优先只在提示区找红色；
                提示区无命中 → 回落全页扫描（并记一条 warning）。

        Returns:
            `SealAnalysis` 列表（按面积降序）。
        """
        from ..aligner import BBoxAligner

        aligner = BBoxAligner()
        candidates = _candidates(image, min_area_ratio=self._settings.seal_min_area_ratio)
        if hints and candidates:
            hint_points = [tuple(float(v) for v in box) for box in hints]
            kept = [
                item
                for item in candidates
                if _touches(
                    _to_point_box(item[0], aligner, page, src_dpi), hint_points
                )
            ]
            if kept:
                candidates = kept
            else:
                import logging

                logging.getLogger("doc_processor.ocr.seal").warning(
                    "提示区域内未命中红色印章，已回落全页扫描"
                )

        boxes_points = [_to_point_box(box, aligner, page, src_dpi) for box, _ in candidates]
        analyses: list[SealAnalysis] = []
        for index, ((box, circularity), points) in enumerate(zip(candidates, boxes_points)):
            overlap = any(
                _iou(points, other) >= 0.5
                for other_index, other in enumerate(boxes_points)
                if other_index != index
            )
            text, ocr_score, engine_available = self._read_text(
                image, box, page=page, src_dpi=src_dpi
            )
            matched, ratio = match_parties(text, party_names)
            region_score = min(1.0, circularity / 0.6)
            confidence = REGION_SCORE_WEIGHT * region_score
            if engine_available:
                confidence += OCR_SCORE_WEIGHT * ocr_score
            status = judge_status(
                region_score=region_score,
                extracted_text=text,
                matched=matched,
                overlap=overlap,
                parties_provided=bool(party_names),
            )
            analyses.append(
                SealAnalysis(
                    detection=SealDetection(
                        page=page,
                        x=points["x"],
                        y=points["y"],
                        width=points["width"],
                        height=points["height"],
                        extracted_text=text,
                        confidence=round(min(1.0, max(0.0, confidence)), 2),
                    ),
                    seal_type=classify_seal_type(text),
                    status=status,
                    is_matched_with_parties=matched,
                    party_ratio=ratio,
                    compliance_note=_note_for(status, ratio, engine_available),
                )
            )
        return analyses

    # ---------------- 内部 ----------------
    def _engine(self) -> OcrEngine | None:
        """取 OCR 引擎（未注入时惰性构造；失败 → None + 只告警一次）。"""
        global _ENGINE_WARNED
        if self._ocr is not None:
            return self._ocr
        from ..errors import OcrEngineUnavailableError
        from .engine import build_ocr_engine

        try:
            self._ocr = build_ocr_engine(self._settings)
        except OcrEngineUnavailableError:
            if not _ENGINE_WARNED:
                _ENGINE_WARNED = True
                import logging

                logging.getLogger("doc_processor.ocr.seal").warning(
                    "OCR 引擎不可用：印章只做区域检测，文字置空（不抛异常）"
                )
            return None
        return self._ocr

    def _read_text(
        self, image: np.ndarray, box: tuple[int, int, int, int], *, page: int, src_dpi: int
    ) -> tuple[str | None, float, bool]:
        """极坐标展开 + 四方向 OCR。

        Returns:
            `(文本, 最高置信度, 引擎是否可用)`；引擎不可用 → `(None, 0.0, False)`。
        """
        engine = self._engine()
        if engine is None:
            return None, 0.0, False
        import cv2

        strip = expand_ring(image, box, steps=self._settings.seal_ring_steps)
        if strip.size == 0:
            return None, 0.0, True
        best_text, best_score = None, 0.0
        for rotation in _ROTATIONS:
            operation = _ROTATION_OPS[rotation]
            rotated = strip if operation is None else cv2.rotate(strip, getattr(cv2, operation))
            try:
                results = engine.ocr_image(rotated, page=page, src_dpi=src_dpi)
            except Exception:  # noqa: BLE001 - 引擎中途不可用：与「未接入」同样处理
                return None, 0.0, False
            if not results:
                continue
            score = max(item.confidence for item in results)
            if score > best_score:
                best_text = "".join(item.text for item in results)
                best_score = score
        if best_score < self._settings.min_confidence:
            return None, 0.0, True
        return best_text, best_score, True


def _to_point_box(
    box: tuple[int, int, int, int], aligner: Any, page: int, src_dpi: int
) -> dict:
    """像素框 → openapi `BBox`（**经唯一缩放处** `to_pdf_points`）。"""
    normalized = aligner.to_pdf_points(page, box[0], box[1], box[2], box[3], src_dpi=src_dpi)
    return {
        "page": normalized.page,
        "x": round(normalized.x, 1),
        "y": round(normalized.y, 1),
        "width": round(normalized.width, 1),
        "height": round(normalized.height, 1),
    }


def _touches(box: dict, hints: list[tuple[float, float, float, float]]) -> bool:
    """候选框（点）是否与任一提示框（`x0, top, x1, bottom`，点）相交。"""
    x0, y0 = box["x"], box["y"]
    x1, y1 = x0 + box["width"], y0 + box["height"]
    for hint_x0, hint_top, hint_x1, hint_bottom in hints:
        if x0 < hint_x1 and x1 > hint_x0 and y0 < hint_bottom and y1 > hint_top:
            return True
    return False


def _note_for(status: str, ratio: float, engine_available: bool) -> str:
    """按状态生成中文复核说明（进报告）。"""
    if status == "SUSPICIOUS_OVERLAP":
        return "印章区域与另一印章重叠（IoU ≥ 0.5），疑为骑缝/重复盖章，需人工复核"
    if status == "BLURRY_UNRECOGNIZABLE":
        suffix = "；本次未接入 OCR 引擎" if not engine_available else ""
        return f"印章文字不可辨（未达 min_confidence），建议调高渲染 DPI 后复检{suffix}"
    if status == "UNAUTHORIZED":
        return f"印章文字与签约主体不匹配（最高相似度 {ratio:.2f}），需核验用章权限"
    if ratio <= 0.0:
        return "已检出印章；未提供主体名称，未做一致性比对"
    return f"印章与签约主体匹配（最高相似度 {ratio:.2f}）"


def detect_seals_in_image(
    image: np.ndarray,
    *,
    page: int,
    src_dpi: int,
    settings: OcrSettings | None = None,
    party_names: Any = (),
    hints: Any = (),
    ocr: OcrEngine | None = None,
) -> list[SealAnalysis]:
    """模块级入口（**3-6 与 3-3 的共同调用点**）。

    Args:
        image: BGR 图像。
        page: 页码（1-based）。
        src_dpi: 图像 DPI。
        settings: 配置；为空取单例。
        party_names: 甲乙方名称。
        hints: 区域提示框（PDF_POINTS）。
        ocr: OCR 引擎（为空时由检测器惰性构造）。

    Returns:
        `SealAnalysis` 列表。
    """
    return LocalSealDetector(settings=settings, ocr=ocr).detect(
        image, page=page, src_dpi=src_dpi, party_names=party_names, hints=hints
    )


def to_detection_payload(analysis: SealAnalysis, *, seq: int) -> dict:
    """转 openapi `SealSignatureDetection` 形状（camelCase + `BBox`，**唯一映射处**）。

    Args:
        analysis: 判定结果。
        seq: 同页序号（从 1 起，用于生成 `id`）。

    Returns:
        可直接 JSON 化的 dict。
    """
    detection = analysis.detection
    return {
        "id": f"seal-{detection.page:03d}-{seq:02d}",
        "type": analysis.seal_type,
        "status": analysis.status,
        "bbox": {
            "page": detection.page,
            "x": round(detection.x, 1),
            "y": round(detection.y, 1),
            "width": round(detection.width, 1),
            "height": round(detection.height, 1),
            "coordinateSystem": "PDF_POINTS",
        },
        "page": detection.page,
        "extractedText": detection.extracted_text,
        "isMatchedWithParties": analysis.is_matched_with_parties,
        "complianceNote": analysis.compliance_note,
        "confidence": detection.confidence,
    }


__all__ = [
    "ASPECT_RANGE",
    "HSV_RED_HIGH",
    "HSV_RED_LOW",
    "MAX_SEALS_PER_PAGE",
    "MIN_CIRCULARITY",
    "MORPH_KERNEL_SIZE",
    "OCR_SCORE_WEIGHT",
    "PADDING_RATIO",
    "PARTY_MATCH_THRESHOLD",
    "REGION_SCORE_WEIGHT",
    "LocalSealDetector",
    "SealAnalysis",
    "classify_seal_type",
    "detect_seals_in_image",
    "expand_ring",
    "find_seal_regions",
    "judge_status",
    "match_parties",
    "red_mask",
    "to_detection_payload",
]
