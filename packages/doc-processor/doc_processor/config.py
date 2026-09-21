"""OCR / 文档解析配置：`CG_OCR_*` 环境变量（**模块 3 全部配置项的唯一处**，`3-2 §⑤`）。

设计要点：
1. 统一 `env_prefix="CG_OCR_"`，与项目既有 `CG_` 约定一致。
2. **默认值即可离线跑通**：引擎 `auto`（本机落到随包分发的 RapidOCR）、渲染 `pypdfium2`。
3. 🔴 **每一项都必须有消费方**（标准 §0.4）。字段后的注释写明消费任务；
   3-7 / 3-8 的**规则阈值是常量**（不是可调业务参数），**不进本文件**。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OcrSettings(BaseSettings):
    """OCR / 文档解析全部可调参数。

    环境变量形如 `CG_OCR_RENDER_DPI`（前缀 + 字段名大写）。
    """

    model_config = SettingsConfigDict(
        env_prefix="CG_OCR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 分流（消费方：3-2 DocRouter）----
    native_text_min_chars: int = Field(
        default=50,
        ge=0,
        description="采样页平均可抽字符数 ≥ 此值 → PDF_NATIVE（数字版直抽）",
    )
    route_sample_pages: int = Field(
        default=3, ge=1, description="采样页数（首 / 中 / 末）；实际页数不足则全取"
    )

    # ---- 引擎与渲染（消费方：3-3）----
    engine: Literal["auto", "rapid", "paddle"] = Field(
        default="auto", description="OCR 引擎：auto 按可用性择一；rapid / paddle 强制指定"
    )
    render_dpi: int = Field(
        default=200, ge=36, description="PDF → 图像 DPI；**坐标归一必须用它**"
    )
    image_dpi: int = Field(
        default=72, ge=1, description="独立图片的 DPI 假设（1 像素 = 1 点）"
    )
    lang: str = Field(default="ch", description="OCR 语言（传给引擎）")
    min_confidence: float = Field(
        default=0.5, ge=0.0, le=1.0, description="低于此值的 OCR 结果丢弃"
    )
    model_dir: str | None = Field(
        default=None, description="引擎模型目录（离线场景必填；RapidOCR 随包分发时为 None）"
    )
    timeout_s: int = Field(default=60, ge=1, description="单页 OCR 超时（秒）")

    # ---- 印章（消费方：3-8）----
    seal_min_area_ratio: float = Field(
        default=0.0005, ge=0.0, description="红色区域最小面积占比（相对页面面积）"
    )
    seal_ring_steps: int = Field(
        default=360, ge=8, description="印章极坐标展开的采样角数"
    )


@lru_cache(maxsize=1)
def get_ocr_settings() -> OcrSettings:
    """获取（进程内缓存的）配置单例。

    Returns:
        `OcrSettings` 实例。单测可用 `get_ocr_settings.cache_clear()` 重置。
    """
    return OcrSettings()


__all__ = ["OcrSettings", "get_ocr_settings"]
