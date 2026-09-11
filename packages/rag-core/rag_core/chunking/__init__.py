"""切片（chunking）模块。

Phase 1 只落地 `SimpleChunker`（按条款/法条编号正则切）。
Phase 2 的 `SectionAwareChunker`（章/节/条/款/项层级 + 滑动窗口）在 T03 追加，
两者的输出契约 `ContractChunk` **完全一致**，因此替换不影响下游。
"""

from __future__ import annotations

from .simple_chunker import ChunkStats, SimpleChunker, SourceDoc

__all__ = ["ChunkStats", "SimpleChunker", "SourceDoc"]
