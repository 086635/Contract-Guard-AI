"""层级切片器自检（T-A9 · 组件卡 `1.C2` §⑧ / `1-8层级切片器.md` §⑨）。

覆盖：契约一致性（字段名与"除三个层级字段外逐字相同"）、层级填充与路径前缀、
不跨条款边界、窗口子块带父、确定性 id、无法归类的编号不猜层级、共享正则守卫。
全部离线（`HashEmbedder` 都不需要，只是纯切片）。
"""

from __future__ import annotations

import json
from pathlib import Path

from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType
from rag_contracts.patterns import CLAUSE_HEAD_RE
from rag_core.chunking import SectionAwareChunker, SimpleChunker, SourceDoc
from rag_core.chunking.section_aware import build_hierarchy, classify_level

_MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "rag_core" / "chunking" / "section_aware.py"
)
_CORPUS = (
    Path(__file__).resolve().parents[3] / "scripts" / "data" / "phase1_sample.jsonl"
)

# 层级字段（**唯一允许与 SimpleChunker 不同的三个字段**）
_HIERARCHY_FIELDS = {"parent_section_id", "section_path", "level"}
# created_at 是构造时间戳，天然不同，不参与逐字比对
_IGNORED_FIELDS = _HIERARCHY_FIELDS | {"created_at"}

_DOC_TEXT = """前言：本合同由甲乙双方于北京市签订。

第一章 总则
第一条 目的
为规范双方权利义务，特订立本合同。
第二条 定义
本合同所称货物指供方交付的全部产品与配件。
（一）主设备；
（二）备品备件。

第二章 违约责任
第一节 违约金
第三条 违约金标准
买方逾期付款的，应按日千分之五支付违约金。
第四条 违约金上限
违约金总额不超过合同总价的百分之十。

6.2 其他约定
本条使用多级阿拉伯编号，层级无法归类。

第三章 附则
第五条 生效
本合同自双方签字之日起生效。
"""


def _doc(text: str = _DOC_TEXT, doc_id: str = "DOC-1") -> SourceDoc:
    """构造一个标准条款源文档。"""
    return SourceDoc(
        doc_id=doc_id,
        source_type=SourceType.STANDARD_CLAUSE,
        contract_type=ContractType.PURCHASE,
        text=text,
    )


def _by_section(chunks: list[ContractChunk]) -> dict[str, ContractChunk]:
    """按 `section_id` 建索引。"""
    return {c.section_id: c for c in chunks}


# ===========================================================================
# 1. 契约一致性（可互换）
# ===========================================================================
def test_output_contract_unchanged() -> None:
    """§⑨-9 契约守卫：产出的字段集合与 `SimpleChunker` **完全一致**（不多不少）。"""
    aware = SectionAwareChunker().chunk(_doc())
    simple = SimpleChunker().chunk(_doc())

    assert aware and simple
    assert all(isinstance(c, ContractChunk) for c in aware)
    assert set(ContractChunk.model_fields) == set(type(simple[0]).model_fields)
    assert len(ContractChunk.model_fields) == 28


def test_extra_fields_identical_to_simple_chunker() -> None:
    """§⑥-3 可互换：**除三个层级字段外**，两者输出逐字相同（含 chunk_id）。"""
    simple = SimpleChunker(max_chars=200, window_overlap=20).chunk(_doc())
    aware = SectionAwareChunker(
        max_chars=200, window_chars=200, window_overlap=20
    ).chunk(_doc())

    assert [c.chunk_id for c in aware] == [c.chunk_id for c in simple]
    for a, s in zip(aware, simple):
        a_dump = a.model_dump(exclude=_IGNORED_FIELDS)
        s_dump = s.model_dump(exclude=_IGNORED_FIELDS)
        assert a_dump == s_dump, f"除层级字段外出现差异：{a.section_id}"


# ===========================================================================
# 2. 层级填充（level / section_path / parent）
# ===========================================================================
def test_level_mapping_matches_spec() -> None:
    """§⑥ 映射表：章=1 / 节=2 / 条=3；契约 `level` 取值合法。"""
    chunks = _by_section(SectionAwareChunker().chunk(_doc()))

    assert chunks["第一章"].level == 1
    assert chunks["第一节"].level == 2
    assert chunks["第一条"].level == 3
    assert chunks["第四条"].level == 3
    assert all(0 <= c.level <= 8 for c in chunks.values())


def test_level_and_path_filled() -> None:
    """§⑧-2：条的 `section_path` 含章级路径，父指针指向节。"""
    chunks = _by_section(SectionAwareChunker().chunk(_doc()))
    clause = chunks["第三条"]

    assert clause.section_path == "第二章/第一节/第三条"
    assert clause.parent_section_id == "第一节"
    assert chunks["第一章"].parent_section_id is None
    assert chunks["第一章"].section_path == "第一章"


def test_section_path_is_prefix_of_child() -> None:
    """§⑧-3：子块 `section_path` 以父块为前缀。"""
    chunks = _by_section(SectionAwareChunker().chunk(_doc()))
    for chunk in chunks.values():
        if chunk.parent_section_id is None or chunk.level == 0:
            continue
        parent = chunks.get(chunk.parent_section_id)
        assert parent is not None, f"父块缺失：{chunk.section_id}"
        assert chunk.section_path.startswith(f"{parent.section_path}/")


def test_parent_link_is_resolvable() -> None:
    """§⑧-2（任务层）：每个非空 `parent_section_id` 都能在同一批输出里找到对应切片。"""
    chunks = SectionAwareChunker().chunk(_doc())
    ids = {c.section_id for c in chunks}
    for chunk in chunks:
        if chunk.parent_section_id is None:
            continue
        if chunk.chunk_type is ChunkType.WINDOW_SLICE:
            # 窗口子块的父是宿主锚点（不带 #slice 后缀）
            assert chunk.parent_section_id in ids
            continue
        assert chunk.parent_section_id in ids, chunk.section_id


def test_section_path_has_no_title_text() -> None:
    """§⑧-7：`section_path` 只拼稳定编号，不含标题里会变的文字。"""
    chunks = SectionAwareChunker().chunk(_doc())
    for chunk in chunks:
        assert "违约责任" not in chunk.section_path
        assert "总则" not in chunk.section_path
        assert "附则" not in chunk.section_path


# ===========================================================================
# 3. 红线：不跨条款边界 + 窗口子块带父
# ===========================================================================
def test_no_cross_clause_boundary() -> None:
    """§⑧-4（最重要）：普通切片只含 **1 个**编号行（前言块与窗口块除外）。"""
    for chunk in SectionAwareChunker().chunk(_doc()):
        if chunk.chunk_type is ChunkType.WINDOW_SLICE or chunk.section_id == "PREAMBLE":
            continue
        heads = [
            line
            for line in chunk.text.splitlines()
            if CLAUSE_HEAD_RE.match(line.strip())
        ]
        assert len(heads) <= 1, f"切片跨越了条款边界：{chunk.section_id} / {heads}"
        assert heads, f"非前言块应以编号行开头：{chunk.section_id}"


def test_long_block_window_slice_keeps_parent() -> None:
    """§⑧-5 + `1-8 §⑥`：超长块退化 `WINDOW_SLICE`，带父指针，且 level/path 随宿主条款。"""
    long_text = "第三条 超长条款\n" + "违约金按日千分之五计算。" * 40
    chunker = SectionAwareChunker(max_chars=200, window_chars=150, window_overlap=20)
    chunks = chunker.chunk(_doc(f"第一章 总则\n{long_text}"))

    windows = [c for c in chunks if c.chunk_type is ChunkType.WINDOW_SLICE]
    assert len(windows) >= 2, "超长块应退化为多个窗口子块"
    for window in windows:
        assert window.parent_section_id == "第三条"
        assert window.level == 3, "窗口子块层级随宿主条款"
        assert window.section_path == "第一章/第三条"
        assert window.section_id.startswith("第三条#")
    assert [c.seq_in_doc for c in windows] == sorted(c.seq_in_doc for c in windows)


def test_window_slice_has_parent_for_all() -> None:
    """任务 §⑧-5：输出中 **所有** `WINDOW_SLICE` 都带 `parent_section_id`。"""
    documents = [
        _doc("第一章 总则\n第一条 超长\n" + "内容" * 300),
        _doc("前言\n" + "说明" * 300),  # 前言块超长（无编号）
        _doc("6.2 多级编号超长\n" + "条款" * 300),
    ]
    chunker = SectionAwareChunker(max_chars=120, window_chars=100, window_overlap=10)
    for doc in documents:
        for chunk in chunker.chunk(doc):
            if chunk.chunk_type is ChunkType.WINDOW_SLICE:
                assert chunk.parent_section_id, f"窗口子块缺父：{chunk.section_id}"


# ===========================================================================
# 4. 未知编号 / 空文档 / 确定性
# ===========================================================================
def test_unknown_number_level_zero() -> None:
    """§⑥-5 宁缺勿造：多级阿拉伯编号（如 `6.2`）→ `level=0`、不编造 path。"""
    chunks = _by_section(SectionAwareChunker().chunk(_doc()))
    unknown = chunks["6.2"]

    assert unknown.level == 0
    assert unknown.section_path == ""
    assert unknown.parent_section_id is None
    assert classify_level("6.2") == 0
    assert classify_level("3.1.2") == 0


def test_preamble_level_zero() -> None:
    """前言块（无编号）同样 `level=0` 且不编造 path。"""
    chunks = _by_section(SectionAwareChunker().chunk(_doc()))
    assert chunks["PREAMBLE"].level == 0
    assert chunks["PREAMBLE"].section_path == ""


def test_empty_doc_returns_empty() -> None:
    """§⑨-8：空文档 / 纯空白返回 `[]`，不报错。"""
    chunker = SectionAwareChunker()
    assert chunker.chunk(_doc("")) == []
    assert chunker.chunk(_doc("   \n\n  ")) == []


def test_chunk_id_deterministic() -> None:
    """§⑧-6：同输入两次 `chunk()` 产出**完全相同**（id 列表 + 全量载荷）。"""
    chunker = SectionAwareChunker()
    first = chunker.chunk(_doc())
    second = SectionAwareChunker().chunk(_doc())

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.model_dump(exclude={"created_at"}) for c in first] == [
        c.model_dump(exclude={"created_at"}) for c in second
    ]


def test_hierarchy_helper_is_pure() -> None:
    """内部工具自证：`build_hierarchy` 不改文档、可重复调用。"""
    first = build_hierarchy(_DOC_TEXT)
    second = build_hierarchy(_DOC_TEXT)
    assert first == second
    assert first["第三条"].path == "第二章/第一节/第三条"


# ===========================================================================
# 5. 共享正则守卫 + 真实语料
# ===========================================================================
def test_uses_shared_pattern() -> None:
    """§⑧-7：编号正则必须是 import（**禁止**本地再写一份）。"""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "from rag_contracts.patterns import CLAUSE_HEAD_RE" in source
    assert "re.compile(" not in source


def test_chunk_produces_chunks_on_real_corpus() -> None:
    """任务 §⑧-1：对 `scripts/data/phase1_sample.jsonl` 的语料产出非空切片。"""
    assert _CORPUS.is_file(), f"语料缺失：{_CORPUS}"
    docs = [
        SourceDoc.from_dict(json.loads(line))
        for line in _CORPUS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chunks = SectionAwareChunker().chunk_many(docs)

    assert len(chunks) >= len(docs), "每个文档至少产出一个切片"
    assert all(isinstance(c, ContractChunk) for c in chunks)
    assert any(c.section_path for c in chunks), "至少应有一批切片带层级路径"
