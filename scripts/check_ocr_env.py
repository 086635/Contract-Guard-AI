"""OCR 环境自证脚本（T-C0 / `3-1 §⑤` 的 CLI 契约）。

为什么需要它：`import 成功 ≠ 模型就绪 ≠ 能出字`（`3-1 §⑨-8`）。
本脚本只回答一个问题：**这台机器现在到底能不能跑 OCR，走哪条路线**。

用法：
    python scripts/check_ocr_env.py            # 人类可读
    python scripts/check_ocr_env.py --json     # 机器可读

退出码：
    0 = 至少一个引擎可用；1 = 无可用引擎。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

# 候选引擎：模块名 -> 是否需要推理内核
_ENGINES: tuple[tuple[str, str, str | None], ...] = (
    # (显示名, 模块名, 必需的推理内核模块名；None 表示自包含)
    ("rapidocr", "rapidocr_onnxruntime", None),
    ("paddleocr", "paddleocr", "paddle"),
)


def _version(module_name: str, dist_name: str | None = None) -> str | None:
    """取已安装包的版本号（未安装返回 None）。

    Args:
        module_name: **导入**名（如 `cv2`）。
        dist_name: 发行名；缺省时由导入名推导（`cv2` → `opencv-python` 这类特例必须显式给）。
    """
    if importlib.util.find_spec(module_name) is None:
        return None
    try:
        from importlib.metadata import version

        return version(dist_name or module_name.replace("_", "-"))
    except Exception:  # noqa: BLE001 - 版本号只是展示信息，取不到不影响结论
        return "unknown"


def _rapidocr_models_ready() -> str:
    """RapidOCR 的模型是否随轮子分发（决定能否离线跑）。"""
    spec = importlib.util.find_spec("rapidocr_onnxruntime")
    if spec is None or not spec.submodule_search_locations:
        return "missing"
    models_dir = Path(next(iter(spec.submodule_search_locations))) / "models"
    if models_dir.is_dir() and any(models_dir.glob("*.onnx")):
        return "bundled"
    return "missing"


def _paddleocr_models_ready() -> str:
    """PaddleOCR 的模型：`CG_OCR_MODEL_DIR` 有内容即视为就绪，否则需要首次联网下载。"""
    model_dir = os.getenv("CG_OCR_MODEL_DIR")
    if model_dir and Path(model_dir).is_dir() and any(Path(model_dir).iterdir()):
        return "local"
    return "needs-download"


def probe() -> dict[str, Any]:
    """探测环境并返回结构化结论。

    Returns:
        含 `engine` / `onnxruntime` / `renderer` / `models` / `verdict` / `torch_in_venv`
        等字段的字典；`verdict ∈ {"OK", "UNAVAILABLE"}`。
    """
    available: list[str] = []
    reasons: list[str] = []
    models = "missing"

    for label, module_name, kernel in _ENGINES:
        if importlib.util.find_spec(module_name) is None:
            reasons.append(f"{label}: 未安装")
            continue
        if kernel is not None and importlib.util.find_spec(kernel) is None:
            # 悬空声明：装了包装层却没有推理内核（F4 的成因）
            reasons.append(f"{label} {_version(module_name)}: 缺少推理内核 {kernel}")
            continue
        available.append(f"{label} {_version(module_name)}")
        if label == "rapidocr":
            models = _rapidocr_models_ready()
        elif label == "paddleocr" and models == "missing":
            models = _paddleocr_models_ready()

    return {
        "engine": " | ".join(available) if available else "none",
        "engine_reasons": reasons,
        "onnxruntime": _version("onnxruntime"),
        "renderer": f"pypdfium2 {_version('pypdfium2')}",
        "fixtures": f"pymupdf {_version('pymupdf')}",
        "cv2": _version("cv2", "opencv-python") or _version("cv2", "opencv-python-headless"),
        "numpy": _version("numpy"),
        "models": models,
        "verdict": "OK" if available else "UNAVAILABLE",
    }


def _print_human(info: dict[str, Any]) -> None:
    """人类可读输出（对齐 `3-1 §⑤` 的契约）。"""
    print(f"engine      : {info['engine']}")
    for reason in info["engine_reasons"]:
        print(f"              - {reason}")
    print(f"onnxruntime : {info['onnxruntime']}")
    print(f"renderer    : {info['renderer']}")
    print(f"fixtures    : {info['fixtures']}")
    print(f"cv2 / numpy : {info['cv2']} / {info['numpy']}")
    print(f"models      : {info['models']}")
    print(f"verdict     : {info['verdict']}")


def main(argv: list[str] | None = None) -> int:
    """入口。

    Args:
        argv: 命令行参数（默认取 `sys.argv[1:]`）。

    Returns:
        进程退出码：0 = 至少一个引擎可用，1 = 无可用引擎。
    """
    parser = argparse.ArgumentParser(description="OCR 环境自证（模块 3 / T-C0）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    info = probe()
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        _print_human(info)
    return 0 if info["verdict"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
