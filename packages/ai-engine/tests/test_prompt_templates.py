"""Prompt 模板文件与加载入口单测（T-B7 / C14）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ai_engine.prompts import (
    OPTIONAL_PROMPT_NAMES,
    PROMPT_NAMES,
    load_default_library,
    templates_dir,
)

_PLACEHOLDERS = {
    "classify": {"contract_title", "clause_excerpt"},
    "extract": {"clause_block", "element_keys"},
    "risk": {
        "clause_label",
        "clause_text",
        "contract_type",
        "tolerance",
        "rag_context",
    },
    "conflict": {"pair_block", "contract_type"},
    "rewrite": {
        "instruction",
        "before",
        "recent_dialog",
        "rag_context",
    },
}
_ALL_NAMES = (*PROMPT_NAMES, *OPTIONAL_PROMPT_NAMES)


def _prompt_path(name: str) -> Path:
    return Path(templates_dir()) / f"{name}.prompt"


def _prompt_text(name: str) -> str:
    return _prompt_path(name).read_text(encoding="utf-8")


def _render_args(name: str) -> dict[str, str]:
    return {placeholder: f"{placeholder}-value" for placeholder in _PLACEHOLDERS[name]}


def test_required_templates_exist() -> None:
    missing = [
        name
        for name in (*PROMPT_NAMES, *OPTIONAL_PROMPT_NAMES)
        if not _prompt_path(name).is_file()
    ]
    assert missing == []


def test_placeholders_match_contract() -> None:
    for name in _ALL_NAMES:
        placeholders = re.findall(r"\{\{(\w+)\}\}", _prompt_text(name))
        assert len(placeholders) == len(set(placeholders))
        assert set(placeholders) == _PLACEHOLDERS[name]


def test_render_strict_raises_on_missing_key() -> None:
    library = load_default_library()
    for name in _ALL_NAMES:
        rendered = library.get(name).render(**_render_args(name))
        assert "{{" not in rendered

    kwargs = _render_args("classify")
    kwargs.pop("contract_title")
    with pytest.raises(KeyError):
        library.get("classify").render(**kwargs)


def test_no_hardcoded_legal_or_model_names() -> None:
    forbidden = (
        "第五百八十五条",
        "gpt-",
        "ChatOpenAI",
        "重写如下合同",
    )
    for name in _ALL_NAMES:
        text = _prompt_text(name)
        assert not [item for item in forbidden if item in text]


def test_encoding_utf8_no_bom_lf() -> None:
    for name in _ALL_NAMES:
        raw = _prompt_path(name).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in raw
        decoded = raw.decode("utf-8")
        assert "\ufffd" not in decoded


def test_load_default_library() -> None:
    library = load_default_library()
    for name in PROMPT_NAMES:
        assert library.get(name).name == name
    for name in OPTIONAL_PROMPT_NAMES:
        assert library.get(name).name == name


def test_templates_mention_json_and_missing_hint() -> None:
    for name in _ALL_NAMES:
        text = _prompt_text(name)
        assert "JSON" in text
        # C80：`extract` 模板**有意**不再要求输出【缺失标准对照】——该标记会被当成
        # 一个非词表要素（`standard_reference`）污染要素抽取；它只属于需要法条溯源
        # 的风险/改写等模板。其余模板仍必须保留该提示。
        if name != "extract":
            assert "【缺失标准对照】" in text


def test_optional_extract_template_if_present() -> None:
    if not _prompt_path("extract").is_file():
        pytest.skip("extract.prompt 为可选模板")
    placeholders = set(re.findall(r"\{\{(\w+)\}\}", _prompt_text("extract")))
    # C80：extract 模板新增 `element_keys`（词表清单由代码注入，模板不硬编码 key）
    assert placeholders == {"clause_block", "element_keys"}


def test_prompt_names_are_ascii_snake() -> None:
    for name in _ALL_NAMES:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name)


def test_template_length_budget() -> None:
    for name in _ALL_NAMES:
        assert len(_prompt_text(name)) <= 1200
