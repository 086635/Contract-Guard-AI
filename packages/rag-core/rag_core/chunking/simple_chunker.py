"""Phase 1 简易切片器：按条款/法条编号正则切（设计文档 §3.2-C）。

红线（设计文档 §2.1）：**绝不跨条款/法条边界切**。
本切片器以"编号行"为锚点分段，天然满足该红线；
段落内部再超长时退化为 `WINDOW_SLICE` 子块，并强制带上 `parent_section_id`。

Phase 2 的 `SectionAwareChunker` 会替换本实现，但输出契约不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from cg_observability import TokenCounter
from rag_contracts.chunk import ContractChunk
from rag_contracts.enums import ChunkType, ContractType, SourceType
from rag_contracts.patterns import CLAUSE_HEAD_RE as _CLAUSE_HEAD_RE

# 条款/法条编号锚点：
#   ① 中文编号：第五条 / 第五章 / 第3章 / 第五百八十五条
#   ② 阿拉伯编号：6.2 / 3.1.2 / 1)
# 🔴 **定义处已迁至 `rag_contracts/patterns.py`（T-C4）**，本文件顶部以
#    `from rag_contracts.patterns import CLAUSE_HEAD_RE as _CLAUSE_HEAD_RE` 引入。
#    迁移只换了"定义位置"，语义**逐字未变**；改它会同时改变切片与解析结果，
#    必须跑两处回归（`packages/rag-core` + `packages/doc-processor`）。

# 无编号的正文前缀（如"鉴于……"）归入前言块
_PREAMBLE_SECTION_ID = "PREAMBLE"


@dataclass
class SourceDoc:
    """待切片的源文档（解析层的输出，或离线导入的 JSONL 行）。

    Attributes:
        doc_id: 源文档标识（合同=contract_id，法规=law_code，范本=file_key）。
        source_type: 检索源类型。
        text: 全文。
        contract_type: 合同类型（合同条款才有）。
        law_code: 法规代码（LAW_ARTICLE 必填）。
        law_title: 法规全称。
        effective_date: 法规生效日。
        category: 业务/风险类别。
        tags: 自由标签。
        page_no: 起始页码。
    """

    doc_id: str
    source_type: SourceType
    text: str
    contract_type: ContractType | None = None
    law_code: str | None = None
    law_title: str | None = None
    effective_date: date | None = None
    category: str = "UNSPECIFIED"
    tags: list[str] = field(default_factory=list)
    page_no: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceDoc:
        """从 JSONL 行构造（自动转换枚举与日期）。

        Args:
            data: 字典，键见类属性；`source_type` / `contract_type` 为字符串枚举值。

        Returns:
            SourceDoc 实例。
        """
        raw_date = data.get("effective_date")
        return cls(
            doc_id=str(data["doc_id"]),
            source_type=SourceType(data["source_type"]),
            text=str(data.get("text", "")),
            contract_type=(
                ContractType(data["contract_type"]) if data.get("contract_type") else None
            ),
            law_code=data.get("law_code"),
            law_title=data.get("law_title"),
            effective_date=(
                date.fromisoformat(raw_date) if isinstance(raw_date, str) else raw_date
            ),
            category=str(data.get("category", "UNSPECIFIED")),
            tags=list(data.get("tags", []) or []),
            page_no=data.get("page_no"),
        )


@dataclass
class ChunkStats:
    """切片统计（埋点必打字段，见设计文档 §3.1.1 `chunk.done`）。"""

    chunk_count: int = 0
    avg_chars: int = 0
    max_chars: int = 0
    min_chars: int = 0
    over_limit_count: int = 0
    avg_tokens: int = 0
    total_tokens: int = 0
    invalid_count: int = 0

    def to_log_fields(self) -> dict[str, int]:
        """转成可直接塞进 `log_event(**fields)` 的字典。"""
        return {
            "chunk_count": self.chunk_count,
            "avg_chars": self.avg_chars,
            "max_chars": self.max_chars,
            "min_chars": self.min_chars,
            "over_limit_count": self.over_limit_count,
            "avg_tokens": self.avg_tokens,
            "token_count": self.total_tokens,
            "invalid_count": self.invalid_count,
        }


class SimpleChunker:
    """按编号正则切分文档，产出 `ContractChunk` 列表。"""

    def __init__(
        self,
        counter: TokenCounter | None = None,
        *,
        max_chars: int = 600,
        min_chars: int = 2,
        window_overlap: int = 0,
    ) -> None:
        """初始化切片器。

        Args:
            counter: Token 计数器；为空则自建（无 tiktoken 时自动估算）。
            max_chars: 单块字符上限，超出则切 `WINDOW_SLICE` 子块。
            min_chars: 低于该长度的块直接丢弃（噪声行）。
            window_overlap: 窗口重叠字符数（Phase 1 默认 0，Phase 2 用 75）。
        """
        self._counter: TokenCounter = counter or TokenCounter()
        self.max_chars: int = max(32, int(max_chars))
        self.min_chars: int = max(0, int(min_chars))
        self.window_overlap: int = max(0, int(window_overlap))

    # ------------------------------------------------------------------
    # 分段
    # ------------------------------------------------------------------
    @staticmethod
    def split_blocks(text: str) -> list[tuple[str, str, list[str]]]:
        """按编号行把全文切成 (section_id, title, lines) 块。

        Args:
            text: 源文档全文。

        Returns:
            块列表：`(section_id, title, 正文行列表)`。
        """
        blocks: list[tuple[str, str, list[str]]] = []
        current_id: str | None = None
        current_title: str = ""
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_id, current_title, current_lines
            if current_id is not None:
                blocks.append((current_id, current_title, current_lines))
            current_id = None
            current_title = ""
            current_lines = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _CLAUSE_HEAD_RE.match(line)
            if match:
                flush()
                current_id = match.group("num").strip()
                rest = match.group("rest").strip()
                # 标题取编号后的前 40 字（去掉常见分隔符）
                current_title = re.sub(r"^[:：、.\s]+", "", rest)[:40]
                current_lines = [line]
            else:
                if current_id is None:
                    current_id = _PREAMBLE_SECTION_ID
                    current_title = ""
                current_lines.append(line)
        flush()
        return blocks

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def chunk(self, doc: SourceDoc) -> list[ContractChunk]:
        """切分单个文档。

        Args:
            doc: 源文档。

        Returns:
            ContractChunk 列表（校验失败的块被跳过）。
        """
        chunks, _ = self.chunk_with_stats(doc)
        return chunks

    def chunk_with_stats(self, doc: SourceDoc) -> tuple[list[ContractChunk], ChunkStats]:
        """切分并返回统计（供埋点 `chunk.done` 使用）。

        Args:
            doc: 源文档。

        Returns:
            (切片列表, 统计信息)。
        """
        base_type = (
            ChunkType.ARTICLE
            if doc.source_type is SourceType.LAW_ARTICLE
            else ChunkType.CLAUSE
        )
        chunks: list[ContractChunk] = []
        stats = ChunkStats()

        for block_idx, (section_id, title, lines) in enumerate(self.split_blocks(doc.text)):
            body = "\n".join(lines).strip()
            if len(body) < self.min_chars:
                continue

            if len(body) > self.max_chars:
                stats.over_limit_count += 1
                pieces = self._window_slice(body)
                for slice_idx, piece in enumerate(pieces):
                    chunk = self._build(
                        doc=doc,
                        section_id=f"{section_id}#{slice_idx}",
                        parent_section_id=section_id,
                        chunk_type=ChunkType.WINDOW_SLICE,
                        title=title or None,
                        text=piece,
                        seq=block_idx * 1000 + slice_idx,
                    )
                    if chunk is not None:
                        chunks.append(chunk)
                    else:
                        stats.invalid_count += 1
            else:
                chunk = self._build(
                    doc=doc,
                    section_id=section_id,
                    parent_section_id=None,
                    chunk_type=base_type,
                    title=title or None,
                    text=body,
                    seq=block_idx,
                )
                if chunk is not None:
                    chunks.append(chunk)
                else:
                    stats.invalid_count += 1

        if chunks:
            chars = [c.char_count for c in chunks]
            tokens = [c.token_count for c in chunks]
            stats.chunk_count = len(chunks)
            stats.avg_chars = sum(chars) // len(chars)
            stats.max_chars = max(chars)
            stats.min_chars = min(chars)
            stats.avg_tokens = sum(tokens) // len(tokens)
            stats.total_tokens = sum(tokens)
        return chunks, stats

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _window_slice(self, text: str) -> list[str]:
        """把超长正文切成等长窗口（可重叠）。

        Args:
            text: 超长正文。

        Returns:
            子块文本列表。
        """
        step = max(1, self.max_chars - self.window_overlap)
        pieces: list[str] = []
        start = 0
        while start < len(text):
            piece = text[start : start + self.max_chars].strip()
            if piece:
                pieces.append(piece)
            if start + self.max_chars >= len(text):
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
        """构造并校验单个 ContractChunk。

        Args:
            doc: 源文档。
            section_id: 切片编号（含 `#slice` 后缀时为窗口子块）。
            parent_section_id: 父编号（窗口子块必填）。
            chunk_type: 切片粒度。
            title: 标题。
            text: 正文。
            seq: 文档内序号。

        Returns:
            校验通过的 ContractChunk；校验失败返回 None（调用方计入 invalid_count）。
        """
        slice_idx = 0
        if "#" in section_id:
            _, _, suffix = section_id.rpartition("#")
            slice_idx = int(suffix) if suffix.isdigit() else 0

        payload: dict[str, Any] = {
            "chunk_id": ContractChunk.make_chunk_id(doc.doc_id, section_id, slice_idx),
            "doc_id": doc.doc_id,
            "source_type": doc.source_type,
            "chunk_type": chunk_type,
            "section_id": section_id,
            "section_path": f"{doc.doc_id}/{section_id}",
            "level": 3 if chunk_type in (ChunkType.CLAUSE, ChunkType.ARTICLE) else 4,
            "seq_in_doc": seq,
            "title": title,
            "text": text,
            "text_hash": ContractChunk.compute_text_hash(text),
            "char_count": len(text),
            "token_count": self._counter.count(text),
            "page_no": doc.page_no,
            "contract_type": doc.contract_type,
            "law_code": doc.law_code,
            "law_title": doc.law_title,
            "article_no": section_id if doc.source_type is SourceType.LAW_ARTICLE else None,
            "effective_date": doc.effective_date,
            "category": doc.category,
            "tags": list(doc.tags),
            "parent_section_id": parent_section_id,
            "embedding_dim": None,  # 尚未向量化
        }
        try:
            return ContractChunk(**payload)
        except Exception:  # noqa: BLE001 - 契约校验失败：丢弃该块（错误码 CG-RAG-3001）
            return None

    def chunk_many(self, docs: Sequence[SourceDoc]) -> list[ContractChunk]:
        """批量切片（按文档顺序编号）。

        Args:
            docs: 源文档序列。

        Returns:
            全部切片（顺序拼接）。
        """
        result: list[ContractChunk] = []
        for doc in docs:
            result.extend(self.chunk(doc))
        return result


__all__ = ["ChunkStats", "SimpleChunker", "SourceDoc"]
