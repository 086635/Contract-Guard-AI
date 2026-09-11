"""RAG 模块全局枚举定义（契约层唯一真理源）。

设计约束（RAG_MODULE_DESIGN.md §3.6）：
1. 所有枚举**集中在本模块**，避免 chunk.py / query.py 各自定义导致的取值漂移与循环导入。
2. 枚举值一律全大写下划线，禁止业务代码使用魔法字符串。
3. `SourceType` 与 `ai_engine.agents.auditor.PreciseCitation.sourceType` 严格一一对应，
   **禁止新增第四值**（新增检索源必须先登记到本枚举）。
"""

from __future__ import annotations

from enum import Enum


class SourceType(str, Enum):
    """检索源类型。与 PreciseCitation.sourceType 一一对应，禁止扩展。

    - STANDARD_CLAUSE：企业标准条款范本（我方基线库）
    - LAW_ARTICLE：法律法规条文（民法典等）
    - PRECEDENT：裁判文书
    """

    STANDARD_CLAUSE = "STANDARD_CLAUSE"
    LAW_ARTICLE = "LAW_ARTICLE"
    PRECEDENT = "PRECEDENT"


class ChunkType(str, Enum):
    """切片粒度（层级切片的层级标识）。

    - SECTION：章/节合并块
    - CLAUSE：合同条款（第 X 条）
    - ARTICLE：法条
    - PARAGRAPH：款
    - ITEM：项
    - TABLE：表格
    - WINDOW_SLICE：超长条款的滑动窗口子块（必须带 parent_section_id）
    - RECITAL：鉴于 / 前言
    """

    SECTION = "SECTION"
    CLAUSE = "CLAUSE"
    ARTICLE = "ARTICLE"
    PARAGRAPH = "PARAGRAPH"
    ITEM = "ITEM"
    TABLE = "TABLE"
    WINDOW_SLICE = "WINDOW_SLICE"
    RECITAL = "RECITAL"


class ContractType(str, Enum):
    """合同类型。与 openapi.yaml ContractType 保持一致。"""

    PURCHASE = "PURCHASE"
    SALES = "SALES"
    NDA = "NDA"
    OUTSOURCING = "OUTSOURCING"
    LABOR = "LABOR"
    OTHER = "OTHER"


class RetrieveStrategy(str, Enum):
    """检索策略。

    - DENSE_ONLY：仅向量路（Phase 1）
    - SPARSE_ONLY：仅 BM25 路（调试用）
    - HYBRID_RRF：双路 + RRF 融合（Phase 2+）
    """

    DENSE_ONLY = "DENSE_ONLY"
    SPARSE_ONLY = "SPARSE_ONLY"
    HYBRID_RRF = "HYBRID_RRF"


class FallbackReason(str, Enum):
    """降级原因。注意：降级不是错误，是设计（缺失检测信号）。"""

    NONE = "NONE"
    OVERLONG_QUERY = "OVERLONG_QUERY"  # 超长文本被拦截/截断
    EMPTY_RECALL = "EMPTY_RECALL"  # 空召回或最高分低于阈值
    TIMEOUT = "TIMEOUT"  # 整体超时
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"  # 向量库/BM25 不可用
    RERANK_FAILED = "RERANK_FAILED"  # 重排失败，降级用融合分
    EMBED_FAILED = "EMBED_FAILED"  # 向量化失败，降级用 BM25 或空


__all__ = [
    "ChunkType",
    "ContractType",
    "FallbackReason",
    "RetrieveStrategy",
    "SourceType",
]
