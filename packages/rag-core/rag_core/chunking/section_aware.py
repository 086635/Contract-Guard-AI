"""层级感知切片器：在 `SimpleChunker` 的分段结果上补齐「章/节/条/款/项」层级（T-A9 / 组件 C2）。

与 `SimpleChunker` 的关系（`1.C2切片器.md` §②/§⑥）：

- **输出契约完全一致**（同为 `list[ContractChunk]`，28 字段一字不改），可互换使用；
- **差异只在三个层级字段**：`parent_section_id` / `section_path` / `level`；
- `section_id` 沿用"稳定锚点"（如 `第六条`），与契约里 `section_id` 的示例
  （`'6.2.1'` / `'第五百八十五条'`）一致 —— 层级走 `section_path`，不污染锚点。

为什么要复用而不是重写：分段（`split_blocks`）与超长退化（`_window_slice`）是既有唯一处，
重写会让"不跨条款边界"这条红线出现第二份实现。本类**只覆写 `_build`**：先让基类按原逻辑
构造（保证除三个层级字段外逐字相同），再用 `model_copy` 改写这三个字段。

🔴 编号正则**唯一处**：`rag_contracts.patterns.CLAUSE_HEAD_RE`（T-C4 迁移）。
本文件**不得**再写一份正则，否则会出现「解析层认为的第 3 条 ≠ 检索层认为的第 3 条」。

对应：`1-8层级切片器.md` §⑥/§⑦；`RAG搭建框架.md` §5（层级切片）。
"""

from __future__ import annotations

from dataclasses import dataclass

from cg_observability import TokenCounter, get_logger, log_event
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType
from rag_contracts.patterns import CLAUSE_HEAD_RE

from .simple_chunker import ChunkStats, SimpleChunker, SourceDoc

_LOG = get_logger("rag_core.chunking.section_aware")

# 前言块（无编号的正文前缀）——与 SimpleChunker 同名同义
_PREAMBLE_SECTION_ID = "PREAMBLE"

# 「第X<字>」的尾字 → 层级（契约 `level` 定义：0=文档，1=章，2=节，3=条，4=款，5=项）
_HEAD_CHAR_LEVEL: dict[str, int] = {
    "章": 1,
    "节": 2,
    "条": 3,
    "款": 4,
    "项": 5,
}


@dataclass(frozen=True)
class SectionInfo:
    """单个锚点的层级信息（内部使用，不进契约）。

    Attributes:
        level: 层级深度（0 表示无法归类，见 `classify_level`）。
        path: 人类可读层级路径（如 `第三章/第六条`）；无法归类时为空串。
        parent: 父级 `section_id`；无父级或无法归类时为 None。
    """

    level: int
    path: str
    parent: str | None


def classify_level(section_id: str) -> int:
    """把锚点编号归类到契约的 `level` 取值。

    规则（`1-8 §⑥` 的映射表，**宁缺勿造**）：

    | 锚点样式 | level |
    |---|---|
    | `第X章` | 1 |
    | `第X节` | 2 |
    | `第X条` | 3 |
    | `第X款` | 4 |
    | `第X项` / `1)` | 5 |
    | 前言块 `PREAMBLE` / 多级阿拉伯编号（`6.2`、`3.1.2`） | **0（无法归类）** |

    多级阿拉伯编号（`6.2`）**故意不猜层级**：它的级深只有作者知道，
    猜错会让 `section_path` 失真；契约里 `level=0` 就是"未分类"的合法取值。

    Args:
        section_id: 锚点编号（或 `PREAMBLE`）。

    Returns:
        层级 0~5。
    """
    if section_id == _PREAMBLE_SECTION_ID:
        return 0
    match = CLAUSE_HEAD_RE.match(section_id)
    if match is None:
        return 0
    num = match.group("num")
    level = _HEAD_CHAR_LEVEL.get(num[-1], 0)
    if level:
        return level
    # 阿拉伯编号：`1)` 视作「项」（§⑥ 映射表）；`6.2` / `3.1.2` 无法归类 → 0
    return 5 if num.endswith(")") else 0


def build_hierarchy(text: str) -> dict[str, SectionInfo]:
    """按文档顺序推导每个锚点的层级信息（父子链 + 路径）。

    算法：维护一条"当前层级链"。遇到 level=L 的锚点时，先弹出链上 level ≥ L 的节点
    （同级或更深者已被它取代），再压入自己；父级 = 链上剩下的最后一个节点。

    Args:
        text: 源文档全文。

    Returns:
        `{section_id: SectionInfo}`；无法归类的锚点 level=0、path=""、parent=None。
    """
    info: dict[str, SectionInfo] = {}
    chain: list[tuple[int, str]] = []
    for section_id, _title, _lines in SimpleChunker.split_blocks(text):
        level = classify_level(section_id)
        if level == 0:
            info[section_id] = SectionInfo(level=0, path="", parent=None)
            continue
        while chain and chain[-1][0] >= level:
            chain.pop()
        parent = chain[-1][1] if chain else None
        info[section_id] = SectionInfo(
            level=level,
            path="/".join([cid for _, cid in chain] + [section_id]),
            parent=parent,
        )
        chain.append((level, section_id))
    return info


class SectionAwareChunker(SimpleChunker):
    """层级感知切片器：识别章/节/条/款/项并建立父子关系。

    与 `SimpleChunker` 的关系：

    - **输出契约完全一致**（同样是 `list[ContractChunk]`），可互换使用；
    - 差异在于它会填充 `parent_section_id` / `section_path` / `level` 三个层级字段
      （`section_id` 仍是稳定锚点，便于跨重建、跨切片器对齐）。

    Attributes:
        window_chars: 超长块退化时的窗口长度（基类用 `max_chars`，本类按 `1-8 §⑥` 分开）。
    """

    def __init__(
        self,
        counter: TokenCounter | None = None,
        *,
        max_chars: int = 1200,
        window_chars: int = 800,
        window_overlap: int = 100,
    ) -> None:
        """初始化。

        Args:
            counter: Token 计数器；为空则自建（与 `SimpleChunker` 同约定，便于互换）。
            max_chars: 单切片字符上限，超出则退化为窗口子块。
            window_chars: 窗口子块的目标长度。
            window_overlap: 窗口子块之间的重叠字符数。
        """
        super().__init__(counter, max_chars=max_chars, window_overlap=window_overlap)
        self.window_chars: int = max(32, int(window_chars))
        self._hierarchy: dict[str, SectionInfo] = {}

    # ------------------------------------------------------------------
    # 主入口（覆写：先算层级，再走基类逻辑）
    # ------------------------------------------------------------------
    def chunk_with_stats(
        self, doc: SourceDoc
    ) -> tuple[list[ContractChunk], ChunkStats]:
        """切分并返回统计（层级已填充 + 打点 `chunk.done`）。

        Args:
            doc: 源文档。

        Returns:
            (切片列表, 统计信息)；统计口径与 `SimpleChunker` 完全一致。
        """
        self._hierarchy = build_hierarchy(doc.text)
        chunks, stats = super().chunk_with_stats(doc)
        levels = [c.level for c in chunks]
        window_slices = sum(1 for c in chunks if c.chunk_type is ChunkType.WINDOW_SLICE)
        depths = [c.section_path.count("/") + 1 for c in chunks if c.section_path]
        log_event(
            _LOG,
            "chunk.done",
            doc_id=doc.doc_id,
            chunks=len(chunks),
            avg_chars=stats.avg_chars,
            # 两个键名都打：卡片 §⑥-6 用 level_max，任务文档 §⑦-8 用 max_level
            max_level=max(levels) if levels else 0,
            level_max=max(levels) if levels else 0,
            path_depth=max(depths) if depths else 0,
            window_slices=window_slices,
        )
        return chunks, stats

    def chunk(self, doc: SourceDoc) -> list[ContractChunk]:
        """把源文档切分为带层级信息的切片序列。

        Args:
            doc: 源文档（与 `SimpleChunker` 同一个 `SourceDoc` 类型）。

        Returns:
            按文档顺序排列的 `ContractChunk` 列表；`parent_section_id` / `section_path` /
            `level` 已按层级填好，其余字段与 `SimpleChunker` 逐字相同。
        """
        return self.chunk_with_stats(doc)[0]

    # ------------------------------------------------------------------
    # 内部：只覆写窗口切法（用 window_chars）与构造（填三个层级字段）
    # ------------------------------------------------------------------
    def _window_slice(self, text: str) -> list[str]:
        """把超长正文切成等长窗口（长度用 `window_chars`，可重叠）。

        Args:
            text: 超长正文。

        Returns:
            子块文本列表。
        """
        step = max(1, self.window_chars - self.window_overlap)
        pieces: list[str] = []
        start = 0
        while start < len(text):
            piece = text[start : start + self.window_chars].strip()
            if piece:
                pieces.append(piece)
            if start + self.window_chars >= len(text):
                break
            start += step
        return pieces

    def _build(
        self,
        *,
        doc: SourceDoc,
        section_id: str,
        parent_section_id: str | None,
        chunk_type: ChunkType,
        title: str | None,
        text: str,
        seq: int,
    ) -> ContractChunk | None:
        """构造单个切片：基类逻辑 + 三个层级字段。

        Args:
            doc: 源文档。
            section_id: 切片编号（窗口子块形如 `第六条#0`）。
            parent_section_id: 基类给出的父编号（窗口子块 = 宿主锚点）。
            chunk_type: 切片粒度。
            title: 标题。
            text: 正文。
            seq: 文档内序号。

        Returns:
            校验通过的 `ContractChunk`；校验失败返回 None（与基类一致）。
        """
        chunk = super()._build(
            doc=doc,
            section_id=section_id,
            parent_section_id=parent_section_id,
            chunk_type=chunk_type,
            title=title,
            text=text,
            seq=seq,
        )
        if chunk is None:
            return None

        anchor = section_id.split("#", 1)[0]
        info = self._hierarchy.get(anchor)
        if info is None or info.level == 0:
            # 无法归类（前言块 / 多级阿拉伯编号）：level=0、不编造 path；
            # 窗口子块仍保留基类给的 parent（契约要求 WINDOW_SLICE 必带 parent）。
            return chunk.model_copy(
                update={
                    "level": 0,
                    "section_path": "",
                    "parent_section_id": parent_section_id,
                }
            )

        update: dict[str, object] = {
            "level": info.level,
            "section_path": info.path,
        }
        if chunk_type is not ChunkType.WINDOW_SLICE:
            # 普通块：父级来自层级链（顶层为 None）
            update["parent_section_id"] = info.parent
        return chunk.model_copy(update=update)


__all__ = ["SectionAwareChunker", "build_hierarchy", "classify_level"]
