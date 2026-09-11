# 合同审查 RAG 体系与 LangGraph Agent 联动技术落地指引文档

> 文档定位：本指引是 **可直接指导代码编写的实现圣经（白皮书级）**，面向本项目 AI/RAG 首席架构师与 Phase 2 核心引擎开发同学。
> 适用范围：企业合同智能审核与风险预警系统（contract-guard-ai，Polyglot Monorepo）。
> 配套契约：`packages/schema/openapi.yaml`、`文件集合/report/data_scheme.md`、`文件集合/report/前端开发指导契约.md`。
>
> 赛题硬指标（须由本 RAG + Agent 体系支撑）：合同类型分类准确率 ≥ 85%；关键要素抽取 F1 ≥ 80%；风险条款识别召回率/精准度 ≥ 75%；审核报告含原文位置、风险等级、修改建议、缺失条款提醒。

---

## 0. 现有工程结构与本指引映射（必读）

本指引严格对齐仓库真实骨架。下表明确标注**每个模块当前状态**，避免"凭空造包"与"覆盖已有代码"两种错误。

| 本指引规划的模块 | 落盘路径 | 当前状态 | 说明 |
|---|---|---|---|
| 元数据 / Chunk 模型 | `packages/rag-etl/rag_etl/models.py` | **待新建** | 本指引定义 |
| DOCX 提取器 | `packages/rag-etl/rag_etl/extractors/docx_extractor.py` | **待新建** | 条款级 AST |
| 条款感知切片器 | `packages/rag-etl/rag_etl/chunking.py` | **待新建** | `ClauseAwareChunker` |
| ETL 编排 | `packages/rag-etl/rag_etl/pipeline.py` | **待新建** | 离线建库入口 |
| Embedding 适配层 | `packages/ai-engine/ai_engine/llm/embedding.py` | **待新建** | `ai_engine/llm/` 目录不存在 |
| 混合检索器 | `packages/ai-engine/ai_engine/rag/retriever.py` | **已存在（stub）** | 已有 `HybridRetriever`/`RetrievedChunk`，本指引填实 |
| 精排重排器 | `packages/ai-engine/ai_engine/rag/reranker.py` | **已存在（stub）** | 已有 `Reranker`，本指引填实 |
| 分层 Memory | `packages/ai-engine/ai_engine/memory/*.py` | **已存在（部分实现）** | `GlobalStateManager` / `ScopedMemoryManager` 已落地，直接复用 |
| Agent State / 图 | `packages/ai-engine/ai_engine/agents/contract_agent.py` | **待新建** | LangGraph `StateGraph` |
| 检索 Tool | `packages/ai-engine/ai_engine/agents/tools.py` | **待新建** | `retrieve_contract_knowledge_tool` |
| 向量存储 | `packages/async-worker/worker/storage/clause_vector_store.py` | **待新建** | 复用已有 `qdrant_client.get_qdrant_client()` |
| BM25 索引 | `packages/async-worker/worker/storage/bm25_store.py` | **待新建** | Jieba + 磁盘持久化 |
| Qdrant 裸工厂 | `packages/async-worker/worker/storage/qdrant_client.py` | **已存在** | 仅连接工厂，被 `ClauseVectorStore` 调用 |
| 配置 | `apps/api-gateway/app/config.py`（同步至 worker） | **待扩展** | 需补 Embedding/LLM/Collection 配置 |
| 解析任务 | `apps/async-worker/worker/tasks/parse_doc.py` | **已存在（stub）** | 串联 `rag-etl` → 双存 |

> 注意：`packages/doc-processor/`（ocr / layout / parser / aligner）负责**原始文本与坐标抽取**，本指引的 `rag-etl` 是其**上游消费者**——`rag-etl` 拿到 `doc_processor` 的纯文本/条款文本后，做"条款感知切片 + 向量/全文双存"。两者职责不重叠：doc-processor 出"字"，rag-etl 出"可检索的条款块"。

---

## 一、RAG 架构总体设计与流转图

### 1.1 离线建库流（Offline Indexing）

```
┌─────────────┐   ┌──────────────────┐   ┌──────────────────────┐   ┌──────────────────────────┐
│ 源文件       │   │ doc_processor    │   │ rag-etl              │   │ async-worker/storage     │
│ (.docx/.pdf/ │──▶│ (OCR/Layout/     │──▶│ Extractor → AST →    │──▶│ ClauseVectorStore(Qdrant)│
│  扫描件)     │   │  Parser → 纯文本)│   │ ClauseAwareChunker   │   │        + BM25Store(磁盘) │
└─────────────┘   └──────────────────┘   └──────────────────────┘   └──────────────────────────┘
                                                        │
                                                        ▼
                                          ClauseChunk{ text, ClauseChunkMeta }
                                          （含 source_doc/clause_id/clause_num/
                                           category/page_num/bbox 六元溯源）
```

要点：
- **单一写入入口**：`parse_doc_task` 调用 `rag_etl.pipeline.build_and_index()`，由它统一产出 `ClauseChunk` 并双写 Qdrant + BM25，保证两路索引的 chunk_id 完全一致。
- **幂等**：以 `clause_id` 为确定性主键（见 §2.3），重复建库不产生孤儿向量。

### 1.2 在线召回流（Online Retrieval）

```
用户/Agent Query
      │
      ▼
┌──────────────── 双路并行检索 ────────────────┐
│  Dense（向量）            Sparse（全文）       │
│  EmbeddingClient          BM25Store(Jieba)    │
│  → Qdrant search top-20   → bm25 top-20       │
└────────────────────┬─────────────────────────┘
                     ▼
              RRF 融合（k=60）→ 统一候选 top-20
                     ▼
        Cross-Encoder Reranker 精排（阈值 0.35）
                     ▼
              截取 Top-3~5 → 组装 retrieved_context
                     ▼
        LangGraph generate_node → LLM（含标准基线 vs 待审条款）
                     ▼
           EnhancedRiskItem / 回答 + PreciseCitation 溯源
```

### 1.3 结构化 Metadata 标准字段（溯源基石）

前端"精准溯源（PreciseCitation）"与后端 RAG 必须共用同一套 Metadata 契约。定义在 `packages/rag-etl/rag_etl/models.py`，同时作为 Qdrant payload 与 BM25 doc 字段：

```python
# packages/rag-etl/rag_etl/models.py
from __future__ import annotations
from pydantic import BaseModel, Field


class BBox(BaseModel):
    """坐标归一化到 PDF 用户空间（72dpi，左上原点）。
    与 doc_processor.aligner.NormalizedBBox 完全同构，前端 PDF.js Overlay 直接消费。"""
    page: int
    x: float
    y: float
    width: float
    height: float
    coordinate_system: str = "PDF_POINTS"


class ClauseChunkMeta(BaseModel):
    """六元溯源标准字段。Qdrant payload 与 BM25 doc 都必须携带此结构。"""
    source_doc: str                 # 文档标识：contract_id 或 MinIO file_key
    clause_id: str                  # 全局唯一条款 ID（确定性 uuid5(clause_num+source_doc)）
    clause_num: str | None = None   # 人类可读编号："第六条" / "6.1" / "Article 8"
    category: str = "UNSPECIFIED"   # 业务/风险类别，用于过滤与分层
    page_num: int | None = None     # 物理页码，供前端跳转
    bbox: BBox | None = None        # 物理位置，供前端高亮
    # —— 辅助检索字段（非六元但强烈建议）——
    contract_type: str | None = None   # 采购/销售/保密/外包/劳动 …，用于语义过滤
    title: str | None = None           # 条款标题，用于 Reranker 展示与报告


class ClauseChunk(BaseModel):
    chunk_id: str                   # == clause_id（单条款未超长时）
    text: str                       # 检索语料正文
    meta: ClauseChunkMeta
```

> 此 `ClauseChunkMeta` 直接喂给 `ai_engine.agents.auditor.PreciseCitation`（`sourceType`/`sourceTitle`/`contentChunk`/`relevanceScore`）：在线召回流把命中的 `ClauseChunk` 映射为 `PreciseCitation`，实现"检索内容来源"的可解释溯源（赛题"可解释"要求）。

---

## 二、离线知识库构建（Offline ELT Pipeline）

### 2.1 提取器设计（`packages/rag-etl/rag_etl/extractors/`）

**职责**：从 `.docx` 解析出**标题层级 + 条款编号**，生成 AST 节点树。复用 `ai_engine.memory.global_state.ClauseNode`（已有：`id/parentId/childrenIds/label/text/level`），不重复造轮子。

```python
# packages/rag-etl/rag_etl/extractors/docx_extractor.py
from __future__ import annotations
import re
from pathlib import Path

from docx import Document  # python-docx>=1.1
from ai_engine.memory import ClauseNode


class DocxClauseExtractor:
    """DOCX 结构解析：按段落样式（Heading 1/2/3）与编号正则还原 AST。"""

    # 匹配条款编号：第六条 / 第6条 / 6.1 / 6.1.2 / Article 8
    _NUM_RE = re.compile(
        r"(?:第\s*[一二三四五六七八九十百千零\d]+\s*条"
        r"|第\s*[一二三四五六七八九十百千零\d]+\s*章"
        r"|\b\d+(?:\.\d+){1,2}\b"
        r"|Article\s+\d+)",
        re.IGNORECASE,
    )

    def extract(self, path: str | Path) -> list[ClauseNode]:
        """返回顶层条款节点列表（含 childrenIds 嵌套）。"""
        doc = Document(str(path))
        roots: list[ClauseNode] = []
        stack: list[tuple[int, ClauseNode]] = []  # (level, node)
        counter = 0

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            level = self._style_level(para)  # Heading1→1, Heading2→2, 正文→3
            node = ClauseNode(
                id=f"clause-{counter}",
                parentId=None,
                childrenIds=[],
                label=text[:40],
                text=text,
                level=level,
            )
            counter += 1
            # 维护父子栈
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                parent = stack[-1][1]
                node.parentId = parent.id
                parent.childrenIds.append(node.id)
            else:
                roots.append(node)
            stack.append((level, node))
        return roots

    @staticmethod
    def _style_level(para) -> int:
        style = (para.style.name or "").lower()
        if "heading 1" in style or style == "title":
            return 1
        if "heading 2" in style:
            return 2
        if "heading 3" in style:
            return 3
        return 4  # 正文/条款正文
```

> PDF 扫描件路径：`doc_processor`（pdfplumber + PaddleOCR）先产出 `text + bbox`，再由本提取器按"编号正则 + 行距"切条款，逻辑同 `DocxClauseExtractor.extract_from_lines()`（签名一致，输入为 `list[str]` 行序列）。

### 2.2 条款感知切片器（`ClauseAwareChunker`）

**设计红线**：
1. 物理边界 = 条款逻辑边界，**绝不**跨条款切。
2. 单条款 ≤ 800 字：整条作为一个 chunk。
3. 单条款 > 800 字：启用 **500 字窗口 / 75 字 overlap** 滑动窗口，每个子块沿用父 `clause_id` 并追加序号后缀。

```python
# packages/rag-etl/rag_etl/chunking.py
from __future__ import annotations
import re
import uuid

from rag_etl.models import BBox, ClauseChunk, ClauseChunkMeta

_WINDOW = 500
_OVERLAP = 75
_HARD_LIMIT = 800

# 仅用于把 AST 平铺为"待切条款文本"列表；编号已在 extractor 阶段识别
_CLAUSE_BOUNDARY = re.compile(
    r"(?:^|\n)\s*(?:第\s*[一二三四五六七八九十百千零\d]+\s*[章条]"
    r"|\b\d+(?:\.\d+){1,2}\b"
    r"|Article\s+\d+)",
    re.IGNORECASE,
)


class ClauseAwareChunker:
    """以条款为物理边界的切片器。"""

    def chunk(
        self,
        *,
        source_doc: str,
        contract_type: str | None,
        full_text: str,
        title: str | None = None,
        page_num: int | None = None,
        bbox: BBox | None = None,
    ) -> list[ClauseChunk]:
        # 1) 按条款边界切分为若干逻辑条款
        boundaries = [m.start() for m in _CLAUSE_BOUNDARY.finditer(full_text)]
        if not boundaries:
            boundaries = [0]
        spans = []
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(full_text)
            spans.append(full_text[start:end].strip())

        out: list[ClauseChunk] = []
        for idx, span in enumerate(spans):
            if not span:
                continue
            base_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_doc}#{idx}"))
            if len(span) <= _HARD_LIMIT:
                out.append(self._make(base_id, span, source_doc, contract_type,
                                      title, page_num, bbox, idx))
            else:
                # 2) 超长：滑动窗口 500/75
                for w, start in enumerate(range(0, len(span), _WINDOW - _OVERLAP)):
                    sub = span[start:start + _WINDOW]
                    if not sub.strip():
                        continue
                    sub_id = f"{base_id}-{w}"
                    out.append(self._make(sub_id, sub, source_doc, contract_type,
                                          title, page_num, bbox, idx))
        return out

    @staticmethod
    def _make(chunk_id, text, source_doc, contract_type, title, page_num, bbox, idx) -> ClauseChunk:
        return ClauseChunk(
            chunk_id=chunk_id,
            text=text,
            meta=ClauseChunkMeta(
                source_doc=source_doc,
                clause_id=chunk_id,
                clause_num=None,           # 由 extractor 阶段回填更精确
                category="UNSPECIFIED",
                page_num=page_num,
                bbox=bbox,
                contract_type=contract_type,
                title=title or f"clause-{idx}",
            ),
        )
```

### 2.3 向量与全文双存（`packages/async-worker/worker/storage/`）

#### 2.3.1 ClauseVectorStore（Qdrant）

```python
# packages/async-worker/worker/storage/clause_vector_store.py
from __future__ import annotations
from qdrant_client import QdrantClient, models as qm

from rag_etl.models import ClauseChunk

COLLECTION = "contract_clauses"   # 建议经 config.CG_QDRANT_COLLECTION 注入
VECTOR_DIM = 1024
DISTANCE = qm.Distance.COSINE


class ClauseVectorStore:
    """Qdrant 向量库封装：建库 / 幂等 upsert / 余弦检索。"""

    def __init__(self, client: QdrantClient, collection: str = COLLECTION) -> None:
        self._c = client
        self._col = collection

    def ensure_collection(self) -> None:
        """建 Collection：1024 维、COSINE；payload 启用 keyword 索引加速过滤。"""
        if self._c.collection_exists(self._col):
            return
        self._c.create_collection(
            collection_name=self._col,
            vectors_config=qm.VectorParams(size=VECTOR_DIM, distance=DISTANCE),
            # payload 索引：category / contract_type / source_doc 作 keyword 过滤
            hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=200),
        )
        self._c.create_payload_index(
            self._col, "category", qm.PayloadSchemaType.KEYWORD)
        self._c.create_payload_index(
            self._col, "contract_type", qm.PayloadSchemaType.KEYWORD)
        self._c.create_payload_index(
            self._col, "source_doc", qm.PayloadSchemaType.KEYWORD)

    def upsert(self, chunks: list[ClauseChunk], vectors: list[list[float]]) -> None:
        """幂等写入：point_id = chunk_id，payload = ClauseChunkMeta 全字段 + text。"""
        assert len(chunks) == len(vectors)
        points = [
            qm.PointStruct(
                id=c.chunk_id,
                vector=vec,
                payload={"text": c.text, **c.meta.model_dump()},
            )
            for c, vec in zip(chunks, vectors)
        ]
        self._c.upsert(collection_name=self._col, points=points)

    def search(self, vector: list[float], top_k: int = 20,
               contract_type: str | None = None) -> list[tuple[str, float, dict]]:
        """余弦检索；可选 contract_type 过滤（同类型优先）。返回 (chunk_id, score, payload)。"""
        filt = None
        if contract_type:
            filt = qm.Filter(must=[
                qm.FieldCondition(field="contract_type",
                                  match=qm.MatchValue(value=contract_type))
            ])
        hits = self._c.search(collection_name=self._col, query_vector=vector,
                              limit=top_k, query_filter=filt)
        return [(h.id, h.score, h.payload) for h in hits]
```

> 调用方式（在 `parse_doc_task` 内）：`ClauseVectorStore(get_qdrant_client()).ensure_collection()` → 用 `EmbeddingClient.embed_batch([c.text for c in chunks])` 取向量 → `upsert(chunks, vectors)`。

#### 2.3.2 BM25Store（全文，Jieba + 磁盘持久化）

采用**自维护倒排索引**（比 whoosh 更可控、依赖更轻），序列化到磁盘并支持按 `source_doc` 增量更新/删除。

```python
# packages/async-worker/worker/storage/bm25_store.py
from __future__ import annotations
import json
import math
import pickle
from collections import defaultdict
from pathlib import Path

import jieba

from rag_etl.models import ClauseChunk

_DEFAULT_DIR = Path("./data/bm25")
_K1 = 1.5
_B = 0.75


class BM25Store:
    """Jieba 分词 + BM25 倒排索引，磁盘 pickle 持久化，按 source_doc 增量更新。"""

    def __init__(self, data_dir: str | Path = _DEFAULT_DIR) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._docs: dict[str, list[str]] = {}          # chunk_id -> tokens
        self._df: defaultdict[str, int] = defaultdict(int)
        self._tf: dict[str, dict[str, int]] = {}        # chunk_id -> {term: freq}
        self._doc_len: dict[str, int] = {}
        self._chunk_source: dict[str, str] = {}        # chunk_id -> source_doc
        self._texts: dict[str, str] = {}               # chunk_id -> 原文（供重排）
        self._avgdl = 0.0
        self._load()

    # —— 持久化 ——
    def _path(self) -> Path:
        return self._dir / "index.pkl"

    def _load(self) -> None:
        if self._path().exists():
            blob = pickle.loads(self._path().read_bytes())
            (self._docs, self._df, self._tf, self._doc_len,
             self._chunk_source, self._texts, self._avgdl) = blob

    def save(self) -> None:
        self._path().write_bytes(pickle.dumps(
            (self._docs, self._df, self._tf, self._doc_len,
             self._chunk_source, self._texts, self._avgdl)))

    # —— 更新机制 ——
    def upsert(self, chunks: list[ClauseChunk]) -> None:
        """增量写入；同一 source_doc 先整篇删除再重加，保证幂等。"""
        for c in chunks:
            self.delete(c.meta.source_doc)
        for c in chunks:
            toks = list(jieba.cut(c.text))
            self._docs[c.chunk_id] = toks
            self._tf[c.chunk_id] = defaultdict(int)
            for t in toks:
                self._tf[c.chunk_id][t] += 1
            self._doc_len[c.chunk_id] = len(toks)
            self._chunk_source[c.chunk_id] = c.meta.source_doc
            self._texts[c.chunk_id] = c.text
            for t in set(toks):
                self._df[t] += 1
        total = sum(self._doc_len.values())
        self._avgdl = total / len(self._doc_len) if self._doc_len else 0.0
        self.save()

    def delete(self, source_doc: str) -> None:
        hit = [cid for cid, src in self._chunk_source.items() if src == source_doc]
        for cid in hit:
            for t, f in self._tf.get(cid, {}).items():
                self._df[t] = max(0, self._df[t] - 1)
            self._docs.pop(cid, None)
            self._tf.pop(cid, None)
            self._doc_len.pop(cid, None)
            self._chunk_source.pop(cid, None)
            self._texts.pop(cid, None)

    # —— 检索 ——
    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float, str]]:
        """返回 (chunk_id, bm25_score, text)，text 供下游 Cross-Encoder 重排。"""
        q_tokens = list(jieba.cut(query))
        N = len(self._docs)
        scores: dict[str, float] = {}
        for qt in q_tokens:
            idf = math.log(1 + (N - self._df.get(qt, 0) + 0.5) /
                           (self._df.get(qt, 0) + 0.5))
            for doc_id, freq in self._tf.items():
                f = freq.get(qt, 0)
                if f == 0:
                    continue
                dl = self._doc_len[doc_id]
                denom = f + _K1 * (1 - _B + _B * dl / self._avgdl)
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * f * (_K1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [(cid, s, self._texts.get(cid, "")) for cid, s in ranked]
```

> 索引持久化方案：`{data_dir}/index.pkl` 单文件 + `save()` 落盘；`parse_doc_task` 每完成一个合同即 `upsert()`（内部先 `delete` 同 `source_doc` 再写），实现**增量更新且可重跑**。大规模场景可改为分片 pickle 或 SQLite；当前赛题数据量（脱敏样例）单文件足够。

---

## 三、在线混合召回与重排引擎（Online Retrieval Engine）

### 3.1 Embedding 适配层（`packages/ai-engine/ai_engine/llm/embedding.py`）

```python
# packages/ai-engine/ai_engine/llm/embedding.py
from __future__ import annotations
from dataclasses import dataclass

import httpx  # 或 openai>=1.30 客户端


@dataclass
class EmbeddingConfig:
    base_url: str          # 兼容 OpenAI / 本地 vLLM / TEI
    api_key: str
    model: str             # 输出 1024 维的 embedding 模型
    dim: int = 1024
    batch_size: int = 32


class EmbeddingClient:
    """批量向量化适配层：32 条/批，映射到 1024 维空间。"""

    def __init__(self, cfg: EmbeddingConfig) -> None:
        self._cfg = cfg
        self._http = httpx.Client(base_url=cfg.base_url,
                                  headers={"Authorization": f"Bearer {cfg.api_key}"})

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入；自动按 batch_size 切片，返回与输入等长、每向量 dim 维。"""
        out: list[list[float]] = []
        for i in range(0, len(texts), self._cfg.batch_size):
            batch = texts[i:i + self._cfg.batch_size]
            resp = self._http.post("/embeddings",
                                   json={"model": self._cfg.model, "input": batch})
            resp.raise_for_status()
            out.extend([d["embedding"] for d in resp.json()["data"]])
        assert all(len(v) == self._cfg.dim for v in out), "维度不符 1024"
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]
```

> 维度校验是红线：`assert len(v) == 1024`，否则 Qdrant upsert 会直接 400。本地可落地方案：TEI（`text-embeddings-inference`）托管 `bge-large-zh`/`bge-m3`（m3 输出 1024 维，与选题一致）；或商用 API。低配 CPU 推理用 `bge-small-zh`（512 维需同步改 `VECTOR_DIM`）。

### 3.2 混合检索器 + RRF（k=60）

**填实已有 `ai_engine/rag/retriever.py`**（保留既有 `RetrievedChunk` 定义）：

```python
# packages/ai-engine/ai_engine/rag/retriever.py （在原有 stub 基础上实现）
from __future__ import annotations
from dataclasses import dataclass

from ai_engine.llm.embedding import EmbeddingClient
from worker.storage.clause_vector_store import ClauseVectorStore
from worker.storage.bm25_store import BM25Store


@dataclass
class RetrievedChunk:
    id: str
    title: str
    text: str
    sourceType: str          # STANDARD_CLAUSE | LAW_ARTICLE | PRECEDENT（落库侧约定）
    score: float             # RRF 融合分（0~1 归一前的原始分）


class HybridRetriever:
    """Dense + Sparse 双路召回，RRF 融合。"""

    def __init__(self, emb: EmbeddingClient, vector_store: ClauseVectorStore,
                 bm25: BM25Store, top_k: int = 8,
                 dense_top: int = 20, sparse_top: int = 20, k: int = 60) -> None:
        self._emb = emb
        self._vs = vector_store
        self._bm25 = bm25
        self._top_k = top_k
        self._dense_top = dense_top
        self._sparse_top = sparse_top
        self._k = k

    def retrieve(self, query: str, contract_type: str | None = None) -> list[RetrievedChunk]:
        q_vec = self._emb.embed_one(query)

        # Dense
        dense = self._vs.search(q_vec, top_k=self._dense_top, contract_type=contract_type)
        # Sparse
        sparse = self._bm25.search(query, top_k=self._sparse_top)

        return self._rrf_fuse(dense, sparse)

    def _rrf_fuse(self, dense, sparse) -> list[RetrievedChunk]:
        """Reciprocal Rank Fusion：score = Σ 1/(k + rank)。"""
        fused: dict[str, float] = {}
        meta: dict[str, tuple[str, str, str]] = {}  # id -> (title, text, sourceType)

        for rank, (cid, score, payload) in enumerate(dense, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (self._k + rank)
            meta[cid] = (payload.get("title", cid),
                         payload.get("text", ""),
                         payload.get("sourceType", "STANDARD_CLAUSE"))
        for rank, (cid, _bm25_score, bm25_text) in enumerate(sparse, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (self._k + rank)
            if cid not in meta:
                meta[cid] = (cid, bm25_text, "STANDARD_CLAUSE")

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:self._top_k]
        return [RetrievedChunk(id=cid, title=meta[cid][0], text=meta[cid][1],
                               sourceType=meta[cid][2], score=s) for cid, s in ranked]
```

### 3.3 精排重排器（`Reranker`，Cross-Encoder）

**填实已有 `ai_engine/rag/reranker.py`**：

```python
# packages/ai-engine/ai_engine/rag/reranker.py （在原有 stub 基础上实现）
from __future__ import annotations
from sentence_transformers import CrossEncoder  # BAAI/bge-reranker-v2-m3

from .retriever import HybridRetriever, RetrievedChunk

_RERANK_THRESHOLD = 0.35   # 低于此分判定"无匹配标准基线 / 缺失"


class Reranker:
    """Cross-Encoder 精排：对 (query, chunk) 算相关性，截取 Top-3~5。"""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3",
                 top_n: int = 5) -> None:
        self._model = CrossEncoder(model_name)
        self._top_n = top_n

    def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        if not chunks:
            return []
        pairs = [(query, c.text) for c in chunks]
        scores = self._model.predict(pairs)  # numpy float32 数组
        for c, s in zip(chunks, scores):
            c.score = float(s)               # 覆写为 Cross-Encoder 精排分
        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:self._top_n]

    @staticmethod
    def is_absent(best_score: float) -> bool:
        """业务判定：最高分 < 0.35 → 待审条款在知识库中找不到可对照标准，判缺失/无风险。"""
        return best_score < _RERANK_THRESHOLD
```

> **商用 API 备选方案**：若无法本地加载 Cross-Encoder（显存受限），改用重排 API（如兼容 `cohere rerank` / 自建 rerank 服务），接口签名不变，仅 `_model.predict` 替换为 HTTP 调用。
> **阈值语义**：`0.35` 是"标准基线匹配"的判据。在 §四 的 `generate_node` 中，若 `Reranker.is_absent(best)` 为真，则输出"缺失条款提醒"或"无参考基线，按通用法理评估"，直接支撑赛题"缺失检测与缺失条款提醒"。

---

## 四、基于 LangGraph Agent 的知识库联动与业务问答

> **核心区分**（用户重点要求）：
> - **纯知识库 = 离线建库 + 在线检索**（第二、三章），是一组**无状态**的存储/检索原语。
> - **LangGraph Agent = 运行时编排**，把 `HybridRetriever` 当 Tool 调用，把检索结果塞进 `AgentState.retrieved_context`，再驱动 LLM 生成"带溯源的审查结论"。两者通过 `retrieve_contract_knowledge_tool` 这一唯一边界解耦。

### 4.1 Tool 封装（`packages/ai-engine/ai_engine/agents/tools.py`）

```python
# packages/ai-engine/ai_engine/agents/tools.py
from __future__ import annotations
from langchain_core.tools import tool

from ai_engine.rag.retriever import HybridRetriever
from ai_engine.rag.reranker import Reranker


def build_retrieval_tool(retriever: HybridRetriever, reranker: Reranker):
    """把混合检索+重排封装为标准 LangChain/LangGraph Tool。"""

    @tool("retrieve_contract_knowledge")
    def retrieve_contract_knowledge_tool(query: str, contract_type: str | None = None) -> str:
        """从企业标准条款库 / 《民法典》 / 内部规章中检索与 query 相关的条款原文。
        入参: query=待审条款文本或用户问题; contract_type=可选合同类型过滤。
        返回: 已重排的 Top-N 条款文本（含来源标识），供 LLM 引用。"""
        candidates = retriever.retrieve(query, contract_type=contract_type)
        ranked = reranker.rerank(query, candidates)
        blocks = []
        for i, c in enumerate(ranked, 1):
            blocks.append(f"[{i}] ({c.sourceType}) {c.title}\n{c.text}")
        return "\n\n".join(blocks)

    return retrieve_contract_knowledge_tool
```

### 4.2 Agent State 状态设计

```python
# packages/ai-engine/ai_engine/agents/contract_agent.py
from __future__ import annotations
from typing import Annotated, TypedDict
import operator

from langgraph.graph import StateGraph, END


class AgentState(TypedDict, total=False):
    """LangGraph 运行时状态。"""
    query: str                      # 用户原始输入 / 待审条款
    contract_type: str | None      # 合同类型（分类模块产出）
    intent: str                    # router 判定: CHITCHAT | RISK_DIAGNOSIS | CLAUSE_REWRITE
    retrieved_context: str         # ★ 检索结果（retrieve_node 写入）
    messages: Annotated[list, operator.add]   # 对话历史
    answer: str                    # generate_node 产出
```

### 4.3 Graph 节点与边流转（StateGraph + Conditional Edges）

```python
# packages/ai-engine/ai_engine/agents/contract_agent.py （续）
from ai_engine.rag.retriever import HybridRetriever
from ai_engine.rag.reranker import Reranker
from ai_engine.agents.tools import build_retrieval_tool


def _router_node(state: AgentState) -> AgentState:
    """Node 1：意图路由。可用 LLM 分类或规则（关键词命中'改写/修改'→REWRITE）。"""
    intent = _classify_intent(state["query"])   # 见下；也可直接 LLM
    return {"intent": intent}


def _retrieve_node(state: AgentState) -> AgentState:
    """Node 2：触发检索 Tool，结果写入 retrieved_context。"""
    tool = build_retrieval_tool(_RETRIEVER, _RERANKER)
    ctx = tool.invoke({"query": state["query"], "contract_type": state["contract_type"]})
    return {"retrieved_context": ctx}


def _generate_node(state: AgentState) -> AgentState:
    """Node 3：组装'标准基线 vs 待审条款' Prompt，调 LLM 生成带溯源结论。"""
    prompt = _build_audit_prompt(
        query=state["query"],
        context=state["retrieved_context"],
    )
    answer = _call_llm(prompt)     # 复用 ai_engine.llm 的 LLM 客户端
    # 若 Reranker.is_absent(highest_score) → answer 追加"缺失条款提醒"
    return {"answer": answer}


def build_contract_agent(retriever: HybridRetriever, reranker: Reranker) -> StateGraph:
    global _RETRIEVER, _RERANKER
    _RETRIEVER, _RERANKER = retriever, reranker

    g = StateGraph(AgentState)
    g.add_node("router", _router_node)
    g.add_node("retrieve", _retrieve_node)
    g.add_node("generate", _generate_node)

    g.set_entry_point("router")

    # 条件边：纯聊天不检索；风险诊断/条款改写都需检索
    def _route(state: AgentState) -> str:
        return "end" if state["intent"] == "CHITCHAT" else "retrieve"

    g.add_conditional_edges("router", _route, {"end": END, "retrieve": "retrieve"})
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()
```

> **`retrieved_context` 的下游消费**：`generate_node` 从 `state["retrieved_context"]` 抽取标准条款，与 `state["query"]`（待审条款）拼成对比 Prompt，要求 LLM 输出 `EnhancedRiskItem`（含 `reasoningSteps` + `citations`）。`citations` 的 `contentChunk` 直接来自 `RetrievedChunk.text`，实现**检索来源可点击溯源**（赛题"可解释"）。

### 4.4 业务场景落地

**场景 A：合同条款对比 / 审查（自动送 RAG 找标准模板）**
- 触发：`router_node` 判定 `RISK_DIAGNOSIS`。
- 流程：`query` = 待审条款文本 → `retrieve_node` 从标准条款库召回 Top-5 → `generate_node` 输出"标准基线 vs 待审条款"差异 + `EnhancedRiskItem`（高/中/低）+ 修改建议。
- 缺失检测：若 `Reranker.is_absent(best)` → 标记"缺失标准对照，建议补全"。

**场景 B：法务知识自由问答（"违约金上限是多少？"）**
- 触发：`router_node` 判定 `CHITCHAT` 或普通问答（不强制走 retrieve 但默认走）。
- 流程：`query` = 用户问题 → `retrieve_node` 召回《民法典》+ 企业内部规章 → `generate_node` 输出精确回答 + `PreciseCitation`（`sourceType=LAW_ARTICLE`）。
- 这正是赛题"可解释：结论依据、检索内容来源"的落地形态。

**分层 Memory 衔接（复用已有 `memory` 包）**：
- `GlobalContractState`（全局 AST 拓扑 + 合同元数据）作为 `retrieve_node` 的 `contract_type`/`clause_id` 来源。
- `ScopedClauseMemory`（当前条款近 3-5 轮对话）作为多轮改写（"把违约金上限改为 20%"）的上下文窗口，避免长对话状态丢失——与赛题"分层 Memory：全局状态 + 局部上下文"完全对齐。

---

## 五、配置扩展（`apps/api-gateway/app/config.py` 同步至 worker）

当前 `Settings` 缺 LLM/Embedding/OCR 配置。新增字段（保持 `CG_` 前缀约定）：

```python
# apps/api-gateway/app/config.py 追加
    # ---- Embedding / LLM / RAG ----
    embedding_base_url: str = "http://localhost:8080"   # TEI / vLLM
    embedding_api_key: str = "none"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "none"
    llm_model: str = "qwen2.5-72b-instruct"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    qdrant_collection: str = "contract_clauses"
    bm25_data_dir: str = "./data/bm25"
```

> 这些字段同时被 `ai-engine`（embedding/llm）、`async-worker`（vector/bm25）、Agent 读取；两个进程共用同一份 `.env`（赛题 Docker Compose 已注入）。

---

## 六、单元测试用例编写指引（pytest）

目标：用可量化指标守住 RAG 质量门，直接对齐赛题召回/精准 ≥ 75%、要素 F1 ≥ 80%。

### 6.1 Recall@5（混合召回质量门）

```python
# tests/test_retrieval.py
import pytest
from ai_engine.rag.retriever import HybridRetriever
from ai_engine.rag.reranker import Reranker
from ai_engine.llm.embedding import EmbeddingClient

# 固定 fixture 语料：标准条款库 + 待审条款 + 预期可命中 chunk_id 映射
@pytest.fixture
def retriever(fixture_chunks, fixture_vectors):
    vs = ClauseVectorStore(get_qdrant_client()); vs.ensure_collection()
    vs.upsert(fixture_chunks, fixture_vectors)
    bm25 = BM25Store("./tmp/bm25_test"); bm25.upsert(fixture_chunks)
    return HybridRetriever(EmbeddingClient(cfg), vs, bm25)

def test_recall_at_5(retriever):
    """每个 query 的预期答案 chunk 必须出现在 RRF 融合后 Top-5。"""
    queries = [("违约责任不对等", {"std-liability-03"}),
               ("保密义务过于宽泛", {"std-confidential-07"})]
    for q, gold in queries:
        hits = retriever.retrieve(q)
        top5 = {c.id for c in hits[:5]}
        assert top5 & gold, f"Recall@5 失败: {q} 未命中 {gold}"
    # 量化门禁：整体 Recall@5 ≥ 0.80（代理赛题风险召回 ≥75% 的检索侧要求）
```

### 6.2 Reranker 阈值与"缺失/无风险"判定

```python
# tests/test_reranker.py
def test_reranker_threshold():
    """Cross-Encoder 得分 < 0.35 必须判为缺失/无风险。"""
    reranker = Reranker(top_n=5)
    # 构造一个明显无关 query
    cand = [RetrievedChunk("x", "t", "无关文本", "STANDARD_CLAUSE", 0.0)]
    ranked = reranker.rerank("本条款与知识产权无关的问题", cand)
    assert ranked[0].score < 0.35
    assert Reranker.is_absent(ranked[0].score) is True

def test_reranker_topn_truncate():
    cand = [RetrievedChunk(f"c{i}", f"t{i}", f"文本{i}", "STANDARD_CLAUSE", 0.0)
            for i in range(20)]
    ranked = reranker.rerank("标准违约金条款", cand)
    assert len(ranked) <= 5          # 截断到 Top-3~5
    assert all(ranked[i].score >= ranked[i+1].score for i in range(len(ranked)-1))
```

### 6.3 切片器与双存一致性

```python
# tests/test_chunking_and_store.py
def test_clause_boundary_not_crossed():
    text = "第一条 甲方权利……\n第二条 乙方义务……（超800字长文本）"
    chunks = ClauseAwareChunker().chunk(source_doc="doc-1", contract_type="采购",
                                        full_text=text)
    # 断言：无单个 chunk 跨两条；超长条款产生 -0..-n 子块且 clause_id 前缀一致
    assert all("第二条" not in c.text for c in chunks if c.meta.clause_num == "第一条")

def test_qdrant_bm25_id_parity():
    """双存 chunk_id 必须一致，保证前端溯源可同时命中两路。"""
    assert {c.chunk_id for c in qdrant_chunks} == {c.chunk_id for c in bm25_chunks}
```

> **评估口径约定**：`Recall@5 ≥ 0.80` 作为 RAG 准入门禁；`Reranker` 最高分 `< 0.35` 即判"缺失标准对照 / 无风险基线"，该信号直接驱动 `generate_node` 输出"缺失条款提醒"。两指标共同覆盖赛题风险识别召回/精准 ≥ 75% 的检索侧要求。

---

## 七、落地顺序与里程碑（给开发排期）

| 阶段 | 任务 | 交付物 | 依赖 |
|---|---|---|---|
| M1 | 配置扩展 + `ai_engine/llm/embedding.py` + `rag_etl/models.py` | 向量化可用、元数据契约冻结 | 无 |
| M2 | `rag-etl`（extractor + chunker）+ `ClauseVectorStore` + `BM25Store` | 离线双存跑通 | M1 |
| M3 | `HybridRetriever` + `Reranker` 填实 + pytest 召回/阈值 | 在线召回流达标（Recall@5≥0.8） | M2 |
| M4 | LangGraph `contract_agent` + `retrieve_contract_knowledge_tool` | Agent 联动可运行 | M3 |
| M5 | 接 `parse_doc_task` / `run_audit_task` 串联 | 端到端（上传→检索→审查→报告） | M4 |

> **红线提醒**：所有 RAG 产出（`RetrievedChunk`/`PreciseCitation`）不得脱离 `ClauseChunkMeta` 六元字段；任何新增检索源（裁判文书/内部规章）必须先登记 `sourceType` 并写入同一 Qdrant Collection，前端溯源逻辑才可保持不变。
