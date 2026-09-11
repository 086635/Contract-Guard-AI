"""溯源映射：`ContractChunk` → `CitationOut`（RAG_MODULE_DESIGN.md §2.0.1）。

本模块是 RAG 与前端溯源消费端之间的**唯一转换点**。
`ai_engine.agents.auditor.PreciseCitation` 是消费端契约，
`RetrievedDoc.citation` 必须能直接赋值，因此两者的字段集必须完全相等。

映射规则（设计文档 §2.0.1）：
| PreciseCitation 字段 | 来源                                   | 兜底规则                     |
|----------------------|----------------------------------------|------------------------------|
| sourceType           | ContractChunk.source_type              | 三值枚举，无兜底（必填）     |
| sourceTitle          | title → law_title+article_no → ...     | 逐级兜底，保证非空           |
| contentChunk         | ContractChunk.text                     | 超 800 字截断 + "…"          |
| relevanceScore       | scores.final                           | clamp 到 [0,1]               |
"""

from __future__ import annotations

from .chunk import ContractChunk
from .query import CitationOut

# contentChunk 截断上限：超过则以 "…" 结尾，避免挤占 Prompt 预算
MAX_CONTENT_CHARS: int = 800
_TRUNCATE_SUFFIX: str = "…"


def truncate_text(text: str, limit: int = MAX_CONTENT_CHARS) -> str:
    """按字符数截断文本，超长部分以 "…" 结尾。

    Args:
        text: 原始文本。
        limit: 保留的最大字符数（不含省略号）。

    Returns:
        截断后的文本；未超长时原样返回。
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}{_TRUNCATE_SUFFIX}"


def clamp_score(score: float) -> float:
    """把任意实数得分 clamp 到 [0, 1]（防御 NaN 与越界）。

    Args:
        score: 原始得分。

    Returns:
        [0,1] 区间内的浮点数；NaN 视为 0.0。
    """
    if score != score:  # NaN 自比较为 False，是标准判定手法
        return 0.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return float(score)


def to_citation(
    chunk: ContractChunk,
    score: float,
    *,
    max_content_chars: int = MAX_CONTENT_CHARS,
) -> CitationOut:
    """把切片 + 得分映射为 PreciseCitation 形状的溯源对象。

    Args:
        chunk: 命中的切片契约对象。
        score: 最终相关性得分（会被 clamp 到 [0,1]）。
        max_content_chars: contentChunk 截断上限。

    Returns:
        与 `auditor.PreciseCitation` 字段 1:1 的 CitationOut。
    """
    return CitationOut(
        sourceType=chunk.source_type,
        sourceTitle=chunk.citation_title(),
        contentChunk=truncate_text(chunk.text, max_content_chars),
        relevanceScore=clamp_score(score),
    )


__all__ = [
    "MAX_CONTENT_CHARS",
    "clamp_score",
    "to_citation",
    "truncate_text",
]
