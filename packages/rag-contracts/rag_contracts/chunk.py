"""ContractChunk 契约：切片后的结构化存储单元（RAG_MODULE_DESIGN.md §2.1）。

设计要点：
1. 同一模型同时作为 Qdrant payload / BM25 doc / PG 落库结构，保证三处一致。
2. `chunk_id` 确定性生成（uuid5），重复建库幂等 —— 禁止随机 uuid（会产生孤儿向量）。
3. `source_type` 与 `auditor.PreciseCitation.sourceType` 严格对齐，禁止新增第四值。
4. 共 28 个字段，全部带类型、约束与含义说明；`extra="forbid"` 是契约驱动的第一道防线。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import ChunkType, ContractType, SourceType

# 契约版本：破坏性变更必须递增，用于向量库灰度迁移时识别旧数据
SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
# 项目锁定向量维度（bge-m3）
EMBEDDING_DIM: int = 1024

# uuid5 命名空间：固定常量（URL 命名空间），保证任何进程生成的 chunk_id 一致
_CHUNK_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


class BBox(BaseModel):
    """PDF 坐标（72dpi 用户空间，左上原点）。与 doc_processor.aligner 同构。"""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1, description="物理页码（1-based）")
    x: float = Field(description="左上角 X（PDF_POINTS）")
    y: float = Field(description="左上角 Y（PDF_POINTS）")
    width: float = Field(description="宽度（PDF_POINTS）")
    height: float = Field(description="高度（PDF_POINTS）")
    coordinate_system: Literal["PDF_POINTS"] = Field(
        default="PDF_POINTS", description="坐标系，项目统一 PDF_POINTS"
    )


class ContractChunk(BaseModel):
    """切片契约主体。字段含义见设计文档 §2.1.1 JSON Schema。"""

    # extra="forbid"：禁止静默吞掉拼写错误的字段（契约驱动的第一道防线）
    # str_strip_whitespace：清洗阶段统一去掉首尾空白，避免 text_hash 抖动
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # ---------------- 标识与来源 ----------------
    schema_version: Literal["1.0.0"] = Field(
        default=SCHEMA_VERSION, description="契约版本，破坏性变更必须递增"
    )
    chunk_id: str = Field(
        min_length=8,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-\.]+$",
        description="确定性 uuid5，同时作为 Qdrant point_id",
    )
    doc_id: str = Field(
        min_length=1,
        max_length=200,
        description="源文档标识：合同取 contract_id，法规取 law_code，标准范本取 MinIO file_key",
    )
    source_type: SourceType = Field(
        description="检索源类型，与 PreciseCitation.sourceType 一一对应"
    )
    chunk_type: ChunkType = Field(description="切片粒度（层级标识）")
    section_id: str = Field(
        min_length=1,
        max_length=64,
        description="章节/条款稳定编号，如 '6.2.1' / '第五百八十五条'。层级切片的锚点",
    )

    # ---------------- 法规专属 ----------------
    law_code: str | None = Field(
        default=None, max_length=64, description="法规代码，如 '民法典'。LAW_ARTICLE 时必填"
    )
    law_title: str | None = Field(
        default=None, max_length=200, description="法规全称，用于拼接 sourceTitle"
    )
    article_no: str | None = Field(
        default=None, max_length=64, description="法条号，如 '第五百八十五条'。合同条款为 null"
    )

    # ---------------- 层级 ----------------
    parent_section_id: str | None = Field(
        default=None, max_length=64, description="父级 section_id，构建层级树。顶层为 null"
    )
    section_path: str = Field(
        default="", max_length=512, description="人类可读层级路径，如 '第三章/第六条/6.1'"
    )
    level: int = Field(
        default=0, ge=0, le=8, description="层级深度：0=文档，1=章，2=节，3=条，4=款，5=项"
    )
    seq_in_doc: int = Field(
        ge=0, description="文档内顺序号，用于检索后按原文顺序重排与上下文窗口扩展"
    )
    title: str | None = Field(default=None, max_length=200, description="条款/法条标题")

    # ---------------- 正文与度量 ----------------
    text: str = Field(
        min_length=1,
        max_length=8000,
        description="检索语料正文。红线：绝不跨条款/法条边界切",
    )
    text_hash: str = Field(
        pattern=r"^[0-9a-f]{16}$",
        description="sha256(text)[:16]，用于增量更新去重：hash 未变则跳过 embedding",
    )
    char_count: int = Field(ge=0, description="字符数。埋点必打：监控切片长度分布")
    token_count: int = Field(ge=0, description="Token 数。埋点必打：Prompt 预算与成本核算")

    # ---------------- 溯源 ----------------
    page_no: int | None = Field(default=None, ge=1, description="物理页码（1-based），供 PDF.js 跳转")
    bbox: BBox | None = Field(default=None, description="物理坐标，供前端高亮")
    contract_type: ContractType | None = Field(
        default=None, description="合同类型，Qdrant keyword payload 索引字段"
    )
    category: str = Field(
        default="UNSPECIFIED", max_length=64, description="业务/风险类别标签，用于分层过滤"
    )
    effective_date: date | None = Field(
        default=None, description="法规生效日（YYYY-MM-DD），法条时效性过滤用"
    )
    tags: list[str] = Field(default_factory=list, description="自由标签，如 ['违约金','付款']")

    # ---------------- 向量元信息 ----------------
    embedding_model: str | None = Field(
        default=None, max_length=100, description="生成该向量的模型名，换模型时可据此筛出失效向量"
    )
    embedding_dim: int | None = Field(
        default=EMBEDDING_DIM,
        ge=1,
        description="向量维度，项目锁定 1024；null 表示尚未向量化",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="ISO 8601 UTC 创建时间",
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展字段逃生舱。禁止把需检索/过滤的字段塞进这里（Qdrant 无法建索引）",
    )

    # ------------------------------------------------------------------
    # 契约硬约束（与 §2.1.1 JSON Schema 的 allOf 条件一一对应）
    # ------------------------------------------------------------------
    @model_validator(mode="after")
    def _conditional_required(self) -> ContractChunk:
        """条件必填与全局约束校验。

        Raises:
            ValueError: LAW_ARTICLE 缺 law_code / WINDOW_SLICE 缺 parent_section_id /
                embedding_dim 不等于 1024。
        """
        if self.source_type is SourceType.LAW_ARTICLE and not self.law_code:
            raise ValueError("source_type=LAW_ARTICLE 时 law_code 必填")
        if self.chunk_type is ChunkType.WINDOW_SLICE and not self.parent_section_id:
            raise ValueError("chunk_type=WINDOW_SLICE 时 parent_section_id 必填")
        if self.embedding_dim is not None and self.embedding_dim != EMBEDDING_DIM:
            raise ValueError(f"embedding_dim 必须为 {EMBEDDING_DIM}（项目锁定）")
        return self

    # ------------------------------------------------------------------
    # 工厂方法
    # ------------------------------------------------------------------
    @staticmethod
    def compute_text_hash(text: str) -> str:
        """计算正文指纹：sha256 前 16 位，用于增量更新去重。

        Args:
            text: 切片正文。

        Returns:
            16 位小写十六进制字符串。
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def make_chunk_id(doc_id: str, section_id: str, slice_idx: int = 0) -> str:
        """确定性生成 chunk_id。相同输入永远得到相同输出 → 幂等 upsert。

        Args:
            doc_id: 源文档标识。
            section_id: 章节/条款稳定编号。
            slice_idx: 同一切片内的子块序号（滑动窗口用）。

        Returns:
            uuid5 字符串（32 位十六进制 + 连字符，共 36 字符，符合 8~64 长度约束）。
        """
        return str(uuid.uuid5(_CHUNK_NAMESPACE, f"{doc_id}#{section_id}#{slice_idx}"))

    # ------------------------------------------------------------------
    # 溯源映射：与 auditor.PreciseCitation 对齐
    # ------------------------------------------------------------------
    def citation_title(self) -> str:
        """逐级兜底生成 sourceTitle，保证非空。

        兜底顺序：title → law_title + article_no → law_title → section_path → doc_id#section_id
        """
        if self.title:
            return self.title
        if self.law_title and self.article_no:
            return f"{self.law_title} {self.article_no}"
        if self.law_title:
            return self.law_title
        if self.section_path:
            return self.section_path
        return f"{self.doc_id}#{self.section_id}"

    def to_payload(self) -> dict[str, Any]:
        """写入 Qdrant 的 payload：JSON 安全化 + 剔除不可索引的 extra。

        Returns:
            可直接作为 Qdrant payload 的扁平字典（bbox 已 dict 化，text 保留 ≤8000 字）。
        """
        data = self.model_dump(mode="json")
        data.pop("extra", None)
        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ContractChunk:
        """从 Qdrant payload 反向构建契约对象（检索阶段用）。

        Args:
            payload: 向量库返回的 payload 字典。

        Returns:
            校验通过的 ContractChunk。

        Raises:
            pydantic.ValidationError: payload 与契约不符（应转 ContractValidationError）。
        """
        return cls.model_validate(payload)


__all__ = [
    "BBOX_COORDINATE_SYSTEM",
    "BBox",
    "ChunkType",
    "ContractChunk",
    "ContractType",
    "EMBEDDING_DIM",
    "SCHEMA_VERSION",
    "SourceType",
]

# 便于外部引用（避免硬编码字面量）
BBOX_COORDINATE_SYSTEM: Literal["PDF_POINTS"] = "PDF_POINTS"
