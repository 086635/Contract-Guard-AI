"""生成模块 3 的测试夹具（T-C1 / `3-2 §④`）。

产物（默认写入 `packages/doc-processor/tests/fixtures/`，**可提交进仓库**）：
    native.pdf    5 页纯文本中文 PDF        → 期望 PDF_NATIVE（采样页 [1, 3, 5]）
    scanned.pdf   1 页**无文本层**的栅格化 PDF → 期望 PDF_SCANNED（采样页 [1]）
    scan.png      1 张扫描图（含红色印章样张） → 期望 IMAGE（后续 3-7 / 3-8 也用）
    contract.docx 最小合同 DOCX（段落 + 表格）  → 期望 DOCX

为什么"脚本 + 提交产物"二者都要：脚本保证别人机器可重复生成；
提交产物保证离线、或没装 pymupdf 的机器上测试也能跑（`3-2 §⑧` 的提示）。

⚠️ `pymupdf` 只在本脚本使用，属 **dev 依赖**（`doc-processor/pyproject.toml` 的
`[dependency-groups] dev`）；生产渲染路径统一 `pypdfium2`（模块总纲 §3.4.1）。

用法：
    python scripts/make_ocr_fixtures.py                 # 写到默认目录
    python scripts/make_ocr_fixtures.py --out <目录>
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pymupdf
from docx import Document

_CJK_FONT = "china-s"          # pymupdf 内置中文字体名（无需外部字体文件）
_BODY_SIZE = 11
_TITLE_SIZE = 16

_PAGE_LINES: tuple[str, ...] = (
    "合同编号：CG-2026-0001",
    "甲方：北京某某科技有限公司",
    "乙方：上海某某贸易有限公司",
    "第一条 标的与数量",
    "甲方向乙方采购服务器设备共计一百台，具体型号以附件一为准。",
    "第二条 价款与支付",
    "合同总价为人民币壹佰万元整，乙方应在验收合格后三十日内支付。",
    "第三条 逾期付款违约金",
    "买方逾期付款的，应按日支付合同总价千分之五的违约金，累计不超过百分之十。",
    "第四条 交付与验收",
    "乙方应于合同生效后六十日内交付，甲方应在收到货物后十日内完成验收。",
    "第五条 争议解决",
    "因本合同发生的争议，双方应协商解决；协商不成的，提交北京仲裁委员会仲裁。",
    "甲方（盖章）：                    乙方（盖章）：",
    "签署日期：2026 年 1 月 1 日",
)


def _draw_page(page: pymupdf.Page, title: str) -> None:
    """在页面上写入固定排版的中文正文（用于 native.pdf 与栅格化源页）。

    Args:
        page: pymupdf 页面对象。
        title: 页首标题。
    """
    y = 80.0
    page.insert_text((72, y), title, fontname=_CJK_FONT, fontsize=_TITLE_SIZE)
    y += 28
    for line in _PAGE_LINES:
        page.insert_text((72, y), line, fontname=_CJK_FONT, fontsize=_BODY_SIZE)
        y += 20


def build_native_pdf(path: Path, pages: int = 5) -> Path:
    """生成多页纯文本 PDF（数字版）。"""
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        _draw_page(page, f"采购合同（第 {index + 1} 页 / 共 {pages} 页）")
    doc.save(path)
    doc.close()
    return path


def build_scanned_pdf(
    source_pdf: Path, path: Path, *, dpi: int = 120, jpg_quality: int = 70
) -> Path:
    """把某一页**栅格化**后放进新 PDF —— 产物没有文本层，模拟扫描件。

    内嵌图像走 **JPEG**：无损（PNG/Flate）内嵌会让单页 A4 夹具膨胀到 MB 级
    （实测 4.2 MB），JPEG 约 1/50，符合「单份夹具 ≤ 200 KB」的约定。

    Args:
        source_pdf: 源 PDF（取第 1 页）。
        path: 输出 PDF 路径。
        dpi: 栅格化 DPI。
        jpg_quality: JPEG 质量（夹具够用即可）。

    Returns:
        输出路径。
    """
    src = pymupdf.open(source_pdf)
    pix = src[0].get_pixmap(dpi=dpi)
    src.close()

    out = pymupdf.open()
    page = out.new_page(width=pix.width * 72 / dpi, height=pix.height * 72 / dpi)
    with tempfile.TemporaryDirectory() as tmp_dir:
        jpg = Path(tmp_dir) / "page.jpg"
        pix.save(str(jpg), jpg_quality=jpg_quality)
        page.insert_image(page.rect, filename=str(jpg))
    out.save(path)
    out.close()
    return path


def build_scan_png(source_pdf: Path, path: Path, *, dpi: int = 120) -> Path:
    """生成单张扫描图，并叠加一枚红色圆形印章样张（供 3-6 / 3-7 / 3-8 复用）。"""
    src = pymupdf.open(source_pdf)
    page = src[0]
    page.draw_circle(
        pymupdf.Point(430, 620), 62, color=(0.85, 0.1, 0.1), width=3
    )
    page.insert_text(
        (386, 624), "某某科技", fontname=_CJK_FONT, fontsize=14, color=(0.85, 0.1, 0.1)
    )
    pix = page.get_pixmap(dpi=dpi)
    pix.save(path)
    src.close()
    return path


def build_contract_docx(path: Path) -> Path:
    """生成最小合同 DOCX（段落 + 一张表格），供 3-4 的直抽路径使用。"""
    doc = Document()
    doc.add_heading("采购合同", level=1)
    doc.add_paragraph("甲方：北京某某科技有限公司")
    doc.add_paragraph("乙方：上海某某贸易有限公司")
    doc.add_paragraph("第一条 标的与数量：甲方向乙方采购服务器设备共计一百台。")
    doc.add_paragraph("第二条 价款与支付：合同总价为人民币壹佰万元整。")
    doc.add_paragraph("第三条 逾期付款违约金：买方逾期付款的，应按日支付合同总价千分之五的违约金。")
    table = doc.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "要素"
    table.cell(0, 1).text = "内容"
    table.cell(1, 0).text = "合同金额"
    table.cell(1, 1).text = "人民币壹佰万元整"
    table.cell(2, 0).text = "合同期限"
    table.cell(2, 1).text = "自 2026 年 1 月 1 日至 2026 年 12 月 31 日"
    doc.save(path)
    return path


def main(argv: list[str] | None = None) -> int:
    """入口。

    Args:
        argv: 命令行参数（默认取 `sys.argv[1:]`）。

    Returns:
        进程退出码（0 = 全部生成成功）。
    """
    default_out = (
        Path(__file__).resolve().parents[1]
        / "packages"
        / "doc-processor"
        / "tests"
        / "fixtures"
    )
    parser = argparse.ArgumentParser(description="生成模块 3 测试夹具（T-C1）")
    parser.add_argument("--out", type=Path, default=default_out, help="输出目录")
    args = parser.parse_args(argv)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    native = build_native_pdf(out / "native.pdf")
    build_scanned_pdf(native, out / "scanned.pdf")
    build_scan_png(native, out / "scan.png")
    build_contract_docx(out / "contract.docx")

    for name in ("native.pdf", "scanned.pdf", "scan.png", "contract.docx"):
        item = out / name
        print(f"{name:16s} {item.stat().st_size:>8d} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
