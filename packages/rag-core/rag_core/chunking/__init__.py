"""切片（chunking）模块。

Phase 1 只落地 `SimpleChunker`（按条款/法条编号正则切）。
`SectionAwareChunker`（章/节/条/款/项层级 + 滑动窗口，T-A9 / 组件 C2）已落地：
两者的输出契约 `ContractChunk` **完全一致**（差异只在 `parent_section_id` /
`section_path` / `level` 三个层级字段），因此替换不影响下游；**默认路径仍是 `SimpleChunker`**。
"""

from __future__ import annotations

from .section_aware import SectionAwareChunker
from .simple_chunker import ChunkStats, SimpleChunker, SourceDoc

__all__ = ["ChunkStats", "SectionAwareChunker", "SimpleChunker", "SourceDoc"]
