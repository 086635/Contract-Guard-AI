"""审核输入适配器单测（T-B8 / C10）。"""

from __future__ import annotations

from worker.adapters import to_clause_inputs


def test_adapters_do_not_fake_bbox() -> None:
    result = to_clause_inputs(
        [{"id": "c1", "text": "合同正文", "section_id": "第 1 条"}]
    )
    assert result[0]["clause_id"] == "c1"
    assert result[0]["label"] == "第 1 条"
    assert result[0]["bbox"] is None


def test_adapter_preserves_explicit_bbox() -> None:
    bbox = {
        "page": 1,
        "x": 1.0,
        "y": 2.0,
        "width": 3.0,
        "height": 4.0,
        "coordinateSystem": "PDF_POINTS",
    }
    result = to_clause_inputs([{"clause_id": "c1", "text": "合同正文", "bbox": bbox}])
    assert result[0]["bbox"] == bbox


def test_adapters_skip_invalid_entries() -> None:
    result = to_clause_inputs(
        [
            {"id": "valid", "text": "正文"},
            {"id": "missing-text"},
            {"text": "missing-id"},
            "not-a-dict",  # type: ignore[list-item]
        ]
    )
    assert [item["clause_id"] for item in result] == ["valid"]


def test_adapter_uses_clause_id_as_label_fallback() -> None:
    result = to_clause_inputs([{"clause_id": "c9", "text": "正文"}])
    assert result[0]["label"] == "c9"


def test_adapter_rejects_non_list_payload() -> None:
    assert to_clause_inputs(None) == []  # type: ignore[arg-type]
