"""任务输入适配器。"""

from __future__ import annotations

from typing import Any

from ai_engine.graphs.state import ClauseInput
from cg_observability import get_logger, log_event

_LOG = get_logger("worker.adapters")


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bbox_payload(value: Any) -> dict[str, Any] | None:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        value = model_dump()
    return value if isinstance(value, dict) else None


def to_clause_inputs(payload: list[dict]) -> list[ClauseInput]:
    """把解析层 / 联调 payload 适配为审核图输入。"""
    if not isinstance(payload, list):
        log_event(
            _LOG,
            "audit.adapter.invalid_payload",
            level="WARNING",
            skipped=1,
        )
        return []

    clauses: list[ClauseInput] = []
    skipped = 0
    for item in payload:
        if not isinstance(item, dict):
            skipped += 1
            continue

        clause_id = item.get("clause_id", item.get("id"))
        text = item.get("text")
        if clause_id in (None, "") or text in (None, ""):
            skipped += 1
            continue

        clause_id = str(clause_id)
        clauses.append(
            {
                "clause_id": clause_id,
                "label": str(item.get("label") or item.get("section_id") or clause_id),
                "text": str(text),
                "level": _optional_int(item.get("level")) or 0,
                "parent_id": (
                    str(item["parent_id"])
                    if item.get("parent_id") is not None
                    else None
                ),
                "page_no": _optional_int(item.get("page_no")),
                "bbox": _bbox_payload(item.get("bbox")),
            }
        )

    log_event(
        _LOG,
        "audit.adapter.done",
        accepted=len(clauses),
        skipped=skipped,
    )
    return clauses


__all__ = ["to_clause_inputs"]
