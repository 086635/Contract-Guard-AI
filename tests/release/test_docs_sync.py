"""C74 T-D7 文档同步与卡片证据守卫（离线）。"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CODEX = REPO_ROOT / "文件集合" / "codex"
MODULE_DOC = CODEX / "4.交付模块.md"
PARENT_DOC = CODEX / "4-7正式部署差距与工程化整改.md"
STANDARD_DOC = CODEX / "0.施工标准与定义归属.md"

#: T-D7 映射的组件卡范围（`4.C44` ~ `4.C75`，共 32 张）
FIRST_CARD = 44
LAST_CARD = 75


def _module_rows() -> list[tuple[str, str, str]]:
    """解析 §4.5 子任务索引，返回 (卡文件名, 任务 ID, 状态列)。"""
    rows: list[tuple[str, str, str]] = []
    for line in MODULE_DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| `4.C"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 8:
            continue
        card = cells[0].strip("`")
        task = cells[2]
        if task.startswith("T-D7"):
            rows.append((card, task, cells[-1]))
    return rows


def test_all_td7_cards_have_evidence_or_pending() -> None:
    """每张 T-D7 卡必须落盘，且状态列要么带证据、要么显式标记待实施。"""
    rows = _module_rows()
    assert len(rows) == LAST_CARD - FIRST_CARD + 1, f"T-D7 组件卡数量异常：{len(rows)}"

    numbers = sorted(int(re.search(r"4\.C(\d+)", card).group(1)) for card, _, _ in rows)
    assert numbers == list(range(FIRST_CARD, LAST_CARD + 1))

    for card, _task, status in rows:
        assert (CODEX / card).is_file(), f"{card} 缺少施工文件"
        if status.startswith("✅"):
            assert "（" in status and len(status) > 20, f"{card} 状态缺少实测证据：{status}"
        else:
            assert status.startswith("⏳") or "待实施" in status, f"{card} 状态标记不合法：{status}"
    # 至少 P0 工作包（C44~C69 与 C74）不能是待实施
    pending = {card for card, _task, status in rows if not status.startswith("✅")}
    assert "4.C74T-D7回归与文档收口.md" not in pending


def test_docs_reference_4_7() -> None:
    """模块总纲与每张 T-D7 卡都必须引用父文档 4-7。"""
    parent = PARENT_DOC.name
    assert parent in MODULE_DOC.read_text(encoding="utf-8")
    for card, _task, _status in _module_rows():
        text = (CODEX / card).read_text(encoding="utf-8")
        assert parent in text, f"{card} 未引用父文档 {parent}"


def test_docs_links_and_status() -> None:
    """0.§0.10 引用表与模块总纲的 4.C* 链接必须全部落盘且互相覆盖。"""
    standard = STANDARD_DOC.read_text(encoding="utf-8")
    module = MODULE_DOC.read_text(encoding="utf-8")

    for card, _task, _status in _module_rows():
        assert card in standard, f"0.§0.10 未登记组件卡 {card}"

    for referenced in set(re.findall(r"`(4\.C[^`]+\.md)`", standard)):
        assert (CODEX / referenced).is_file(), f"0.§0.10 引用了不存在的文件：{referenced}"

    for referenced in set(re.findall(r"`(4\.C[^`]+\.md)`", module)):
        assert (CODEX / referenced).is_file(), f"模块总纲引用了不存在的文件：{referenced}"
