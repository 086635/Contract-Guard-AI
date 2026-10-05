from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def module():
    path = Path(__file__).resolve().parents[1] / "import_official_knowledge.py"
    spec = importlib.util.spec_from_file_location("official_import", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def manifest(tmp_path, **overrides):
    text = "第一条 合同正文\n合同义务。"
    (tmp_path / "law.txt").write_bytes(text.encode("utf-8"))
    entry = {
        "doc_id": "law",
        "library": "law",
        "title": "民法典",
        "code": "CIVIL_CODE",
        "scope": "全国",
        "effective_date": "2021-01-01",
        "file": "law.txt",
        "source_url": "https://www.gov.cn/law",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    entry.update(overrides)
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"documents": [entry]}), encoding="utf-8")
    return path


def test_law_normalization_preserves_articles_and_inline_citations(module):
    result = module.normalize_law_text(
        "第五百七十七条 不履行义务。 第五百七十八条 按本法第五百七十七条规定承担责任。"
    )
    assert result.splitlines() == [
        "第五百七十七条 不履行义务。",
        "第五百七十八条 按本法第五百七十七条规定承担责任。",
    ]


def test_laws_remain_universal_with_source_metadata(module, tmp_path):
    _, docs = module.load_sources(manifest(tmp_path))
    assert docs[0].contract_type is None
    assert docs[0].law_title == "民法典"
    assert docs[0].tags == ["https://www.gov.cn/law"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_url": "https://example.com/fake.gov.cn"},
        {"source_url": "http://www.gov.cn/law"},
        {"sha256": "bad"},
        {"file": "../escape.txt"},
    ],
)
def test_untrusted_or_modified_sources_are_rejected(module, tmp_path, overrides):
    with pytest.raises(ValueError):
        module.load_sources(manifest(tmp_path, **overrides))


def test_template_instructions_do_not_become_contract_clauses(module, tmp_path):
    path = manifest(tmp_path, library="standard", contract_types=["PURCHASE"])
    text = "使用说明\n一、供参考\n第一条 价款\n（一）分期付款\n第二条 违约\n赔偿损失。"
    (tmp_path / "law.txt").write_bytes(text.encode("utf-8"))
    content = json.loads(path.read_text(encoding="utf-8"))
    content["documents"][0]["sha256"] = hashlib.sha256(text.encode()).hexdigest()
    path.write_text(json.dumps(content), encoding="utf-8")
    _, docs = module.load_sources(path)
    assert docs[0].text.startswith("第一条")
    assert "使用说明" not in docs[0].text
    assert "价款 （一）分期付款" in docs[0].text
