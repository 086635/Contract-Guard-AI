"""跨模块共享的词法模式（**唯一定义处**，T-C4 / `3-4 §5.1`）。

为什么必须放这里：
1. **解析层与检索层必须用同一份条款编号识别规则** —— 否则会出现
   「解析层认为的第 3 条 ≠ 检索层认为的第 3 条」，溯源与前端高亮会整体错位。
2. 该正则被 `rag_core.chunking.simple_chunker` 与 `doc_processor.parser.extractor`
   **共同 import**，因此只能放在**零重型依赖**的契约层（本包仅依赖 pydantic）。
   放进 `doc_processor` 会让 `rag-core` 反向依赖解析层（违反标准 §0.3）。

迁移纪律（`3-4 §⑤.1`）：本模块的正则**逐字取自**
`rag_core/chunking/simple_chunker.py` 的既有实现，迁移**只允许改变"定义位置"**，
不允许顺手"优化"（改语义 = 改变切片结果 = 触发 RAG 侧回归）。
"""

from __future__ import annotations

import re

# 条款 / 法条编号锚点：
#   ① 中文编号：第五条 / 第五章 / 第3章 / 第五百八十五条
#   ② 阿拉伯编号：6.2 / 3.1.2 / 1)
CLAUSE_HEAD_RE: re.Pattern[str] = re.compile(
    r"^\s*(?P<num>"
    r"第[一二三四五六七八九十百千零〇\d]+[条章节款项]"
    r"|\d+(?:\.\d+)+"
    r"|\d+\)"
    r"|\d+[、．.](?!\d)"
    r"|[一二三四五六七八九十百]+[、．.]"
    r"|[（(][一二三四五六七八九十百\d]+[）)]"
    r")\s*(?P<rest>.*)$"
)

__all__ = ["CLAUSE_HEAD_RE"]
