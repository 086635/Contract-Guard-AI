"""OCR 引擎可用性硬断言（T-C0 / `3-1 §⑧`）。

🔴 本文件的引擎断言**不允许**写成「无引擎就 skip」—— 那会让 F4（声明了包装层却没有
推理内核）重新潜伏。本任务的定义就是「让至少一个引擎可用」，所以它是硬断言。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _spec_present(module_name: str) -> bool:
    """模块是否可被解析（不真正导入，避免触发重型依赖的副作用）。"""
    return importlib.util.find_spec(module_name) is not None


def test_at_least_one_engine_available() -> None:
    """至少一个 OCR 引擎可用：RapidOCR，或 PaddleOCR 且推理内核齐备。"""
    rapid = _spec_present("rapidocr_onnxruntime")
    paddle = _spec_present("paddleocr") and _spec_present("paddle")
    assert rapid or paddle, (
        "无可用 OCR 引擎 —— 模块 3 的 3-3 / 3-6 / 3-7 / 3-8 全部无法验证。"
        "装法：uv add --package doc-processor rapidocr-onnxruntime onnxruntime"
    )


def test_paddleocr_route_only_if_paddle_present() -> None:
    """若 `paddleocr` 可导入，则推理内核 `paddle` 必须也可导入（禁止悬空声明）。

    未选择 PaddleOCR 路线时跳过；`CG_OCR_TEST_PADDLE=1` 时强制断言（`0.§0.6.2`）。
    """
    forced = bool(os.getenv("CG_OCR_TEST_PADDLE"))
    if not _spec_present("paddleocr") and not forced:
        pytest.skip("未选择 PaddleOCR 路线（CG_OCR_TEST_PADDLE 未设置），跳过")
    assert _spec_present("paddleocr"), "CG_OCR_TEST_PADDLE=1 但 paddleocr 不可导入"
    assert _spec_present("paddle"), "声明了 paddleocr 却没有推理内核 paddle（F4）"


def test_no_engine_import_at_package_top_level() -> None:
    """`3-1 §⑥-4`：引擎只能在函数体内延迟导入，模块顶层出现即破坏离线可导入性。"""
    root = Path(__file__).resolve().parents[1] / "doc_processor"
    banned = (
        "import paddleocr",
        "from paddleocr",
        "import rapidocr_onnxruntime",
        "from rapidocr_onnxruntime",
    )
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(
            open(path, encoding="utf-8").read().splitlines(), start=1
        ):
            if line.startswith((" ", "\t")):  # 缩进 = 函数体内，属允许的延迟导入
                continue
            if any(token in line for token in banned):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], f"引擎必须延迟导入：{offenders}"


def test_no_torch_import_in_source() -> None:
    """本仓不得引入 torch（`0.§5.1` 的依赖方向红线；doc-processor 同样适用）。"""
    root = Path(__file__).resolve().parents[1] / "doc_processor"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(
            open(path, encoding="utf-8").read().splitlines(), start=1
        ):
            if line.strip().startswith(("import torch", "from torch")):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], f"不得 import torch：{offenders}"
