#!/usr/bin/env python
"""C80 / T-D8-5：由《合同要素标准输入模板》生成 30 份评测集（**模板是唯一源**）。

为什么要有这个脚本：评测集必须与「标准输入要求」逐字一致——主体用 `甲方：<全称>`、
金额用阿拉伯数字、期限用 `自 … 起至 … 止`。手写 JSONL 极易与模板漂移，故由模板渲染。

用法::

    python scripts/build_quality_gold.py                       # 写 quality_gold.jsonl
    python scripts/build_quality_gold.py --emit-docx <path>    # 另导出空白可填模板

退出码：0 = 成功；2 = 模板缺失/渲染失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = _REPO_ROOT / "scripts" / "data" / "element_input_template.md"
DEFAULT_OUT = _REPO_ROOT / "scripts" / "data" / "quality_gold.jsonl"

#: 六座甲方城市 / 六座乙方城市（保证 30 份合同里主体名各不相同）
_CITIES_A = ("杭州", "上海", "南京", "成都", "武汉", "西安")
_CITIES_B = ("宁波", "广州", "深圳", "重庆", "长沙", "郑州")

#: 五类合同的基础参数（每类生成 6 个变体：换主体、换金额）。
_TYPES: tuple[dict[str, object], ...] = (
    {
        "type": "PURCHASE",
        "label": "采购合同",
        "subject": "甲方向乙方采购服务器设备 100 台，型号 CG-2026。",
        "payment": "合同签订后三个工作日内，甲方一次性支付全部合同价款。",
        "breach": "乙方逾期交货的，每逾期一日按合同总价款的 30% 支付违约金。",
        "jurisdiction": "乙方所在地人民法院",
        "amount": 1200000.0,
        "start": "2026-03-01",
        "end": "2027-02-28",
        "sign": "2026-03-01",
        "risks": ["PAYMENT_TERM_RISK", "PENALTY_TOO_HIGH", "UNFAVORABLE_JURISDICTION"],
    },
    {
        "type": "SALES",
        "label": "销售合同",
        "subject": "甲方向乙方销售家用电器 5000 台。",
        "payment": "乙方在收货后六十日内支付全部货款。",
        "breach": "甲方有权随时单方解除本合同，乙方无权解除；因甲方原因造成乙方损失的，甲方不承担赔偿责任。",
        "jurisdiction": "甲方所在地人民法院",
        "amount": 860000.0,
        "start": "2026-04-01",
        "end": "2027-03-31",
        "sign": "2026-04-01",
        "risks": ["UNILATERAL_TERMINATION", "LIABILITY_IMBALANCE"],
    },
    {
        "type": "NDA",
        "label": "保密协议",
        "subject": "双方就技术资料交换建立保密关系。",
        "payment": "本协议不涉及费用。",
        "breach": "合作过程中产生的全部成果归甲方所有，乙方放弃一切权利主张。",
        "jurisdiction": "甲方所在地人民法院",
        "amount": 0.0,
        "start": "2026-05-01",
        "end": "2029-04-30",
        "sign": "2026-05-01",
        "risks": ["VAGUE_CONFIDENTIALITY", "IP_OWNERSHIP_RISK"],
    },
    {
        "type": "OUTSOURCING",
        "label": "外包合同",
        "subject": "乙方为甲方提供核心系统开发与运维服务。",
        "payment": "甲方应在项目验收完成后第一百八十日支付全部服务费。",
        "breach": "乙方交付成果不符合约定的，应在三十日内免费返工。",
        "jurisdiction": "乙方所在地人民法院",
        "amount": 2400000.0,
        "start": "2026-06-01",
        "end": "2028-05-31",
        "sign": "2026-06-01",
        "risks": ["PAYMENT_TERM_RISK", "MISSING_DISPUTE_CLAUSE"],
    },
    {
        "type": "LABOR",
        "label": "劳动合同",
        "subject": "乙方担任研发工程师岗位，负责产品研发工作。",
        "payment": "甲方按月支付劳动报酬。",
        "breach": "乙方因任何原因给甲方造成损失的，承担无限赔偿责任。",
        "jurisdiction": "甲方所在地劳动仲裁委员会",
        "amount": 240000.0,
        "start": "2026-07-01",
        "end": "2029-06-30",
        "sign": "2026-07-01",
        "risks": ["UNLIMITED_LIABILITY", "FORCE_MAJEURE_ABSENT"],
    },
)

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def load_template() -> str:
    """读模板正文部分（去掉标题与说明，只取「## 二、合同正文模板」之后的内容）。"""
    text = TEMPLATE.read_text(encoding="utf-8")
    marker = "## 二、合同正文模板"
    index = text.find(marker)
    if index < 0:
        raise ValueError("模板缺少「## 二、合同正文模板」小节")
    body = text[index + len(marker) :].strip()
    # 去掉小节标题下的括号说明行（它属于文档说明，不属于合同正文）
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("（"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def render(template: str, values: dict[str, str]) -> str:
    """渲染模板；占位符缺失即报错（不允许静默留空）。"""

    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"模板占位符缺少取值: {key}")
        return values[key]

    return _PLACEHOLDER_RE.sub(repl, template)


def build_rows() -> list[dict]:
    """按 5 类 × 6 变体生成 30 行评测集。"""
    template = load_template()
    rows: list[dict] = []
    index = 0
    for spec in _TYPES:
        for variant in range(6):
            index += 1
            party_a = f"{_CITIES_A[variant]}启明科技有限公司"
            party_b = f"{_CITIES_B[variant]}远洋设备有限公司"
            amount = float(spec["amount"]) + variant * 10000.0  # type: ignore[arg-type]
            values = {
                "contract_type_label": str(spec["label"]),
                "party_a": party_a,
                "party_b": party_b,
                "subject_matter": str(spec["subject"]),
                "amount": f"{amount:.2f}",
                "payment_method": str(spec["payment"]),
                "term_start": str(spec["start"]),
                "term_end": str(spec["end"]),
                "jurisdiction": str(spec["jurisdiction"]),
                "breach_liability": str(spec["breach"]),
                "sign_date": str(spec["sign"]),
            }
            rows.append(
                {
                    "doc_id": f"Q{index:03d}",
                    "contract_type": spec["type"],
                    "text": render(template, values),
                    "gold_elements": [
                        {"key": "party_a", "value": party_a},
                        {"key": "party_b", "value": party_b},
                        {"key": "amount", "value": f"{amount:.2f}"},
                        {"key": "sign_date", "value": str(spec["sign"])},
                        {"key": "term", "value": f"自 {spec['start']} 起至 {spec['end']} 止"},
                        {"key": "jurisdiction", "value": str(spec["jurisdiction"])},
                    ],
                    "gold_risks": [{"category": r} for r in spec["risks"]],  # type: ignore[union-attr]
                    "note": f"template:{spec['type']}#{variant + 1}",
                }
            )
    return rows


def emit_docx(path: Path) -> None:
    """导出空白可填写模板（占位符→下划线），供法务填写。"""
    from docx import Document

    template = load_template()
    blank = _PLACEHOLDER_RE.sub("＿＿＿＿", template)
    document = Document()
    document.add_heading("合同要素标准输入模板", level=1)
    document.add_paragraph(
        "按规范填写可核对的核心要素：主体写全称、金额写阿拉伯数字、期限写起止日期。"
        "缺项不阻断流程，只降低要素抽取 F1。"
    )
    for line in blank.splitlines():
        document.add_paragraph(line) if line.strip() else document.add_paragraph()
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C80：由标准输入模板生成评测集")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--emit-docx", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        rows = build_rows()
    except Exception as exc:  # noqa: BLE001 - CLI 兜底
        print(f"生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"out": str(args.out), "rows": len(rows)}, ensure_ascii=False))

    if args.emit_docx is not None:
        emit_docx(args.emit_docx)
        print(json.dumps({"docx": str(args.emit_docx)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
