"""上下文拼装器：把召回文档拼成可直接注入 Prompt 的文本块。

格式（设计文档 §3.2-C）：
```
[1] (LAW_ARTICLE) 中华人民共和国民法典 第五百八十五条 [section=第五百八十五条]
当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金……

[2] (STANDARD_CLAUSE) 逾期付款违约金 [section=6.2]
买方逾期付款的，应按日支付合同总价千分之五的违约金，累计不超过合同总价的 10%。
```

预算控制：按相关度降序逐条累加 Token，超出 `max_context_tokens` 即停止并置 `truncated=True`。
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

from cg_observability import TokenCounter
from rag_contracts.agent_tool import EMPTY_CONTEXT_HINT  # 单一真理源，禁止在本文件另写一份
from rag_contracts.query import RetrievedDoc


class ContextBuildResult(NamedTuple):
    """上下文拼装结果。

    Attributes:
        context_block: 拼装好的文本块（无召回时为兜底提示语）。
        context_tokens: 文本块的 Token 数。
        truncated: 是否因预算被截断。
        injected_chunks: 实际注入的切片条数。
    """

    context_block: str
    context_tokens: int
    truncated: bool
    injected_chunks: int


class ContextBuilder:
    """按 Token 预算拼装上下文。"""

    def __init__(self, counter: TokenCounter | None = None) -> None:
        """初始化。

        Args:
            counter: Token 计数器；为空则自建。
        """
        self._counter: TokenCounter = counter or TokenCounter()

    @staticmethod
    def format_entry(index: int, doc: RetrievedDoc) -> str:
        """格式化单条上下文条目。

        Args:
            index: 1-based 序号。
            doc: 召回文档。

        Returns:
            形如 `[1] (LAW_ARTICLE) 标题 [section=xxx]\n正文` 的字符串。
        """
        title = doc.title or doc.citation.sourceTitle
        return f"[{index}] ({doc.source_type.value}) {title} [section={doc.section_id}]\n{doc.text}"

    def build(self, docs: Sequence[RetrievedDoc], budget: int) -> ContextBuildResult:
        """拼装上下文。

        Args:
            docs: 已按相关度降序排列的召回文档。
            budget: Token 预算。

        Returns:
            ContextBuildResult。
        """
        if not docs:
            return ContextBuildResult(
                context_block=EMPTY_CONTEXT_HINT,
                context_tokens=self._counter.count(EMPTY_CONTEXT_HINT),
                truncated=False,
                injected_chunks=0,
            )

        budget = max(1, int(budget))
        parts: list[str] = []
        used_tokens = 0
        truncated = False

        for offset, doc in enumerate(docs):
            entry = self.format_entry(offset + 1, doc)
            cost = self._counter.count(entry)
            if parts and used_tokens + cost > budget:
                # 至少保证第一条被注入（第一条最相关），后续超预算即截断
                truncated = True
                break
            parts.append(entry)
            used_tokens += cost

        block = "\n\n".join(parts)
        return ContextBuildResult(
            context_block=block,
            context_tokens=self._counter.count(block),
            truncated=truncated,
            injected_chunks=len(parts),
        )


__all__ = ["EMPTY_CONTEXT_HINT", "ContextBuildResult", "ContextBuilder"]
