# 合同智能审核 RAG 模块落地白皮书与执行指南

> ⚠️ **文档纠偏声明 · 2026-09-21**（本节由文档纠偏流程**追加**，原文**未删改**）
>
> **状态**：🔵 **L5 · 阅读材料** —— 供**理解概念**，**不作为实现依据**。
>
> | # | 说明 |
> |---|---|
> | 1 | §2 已**自行纠正**「切片 schema 叫 `ClauseChunk` → 真名 `ContractChunk`」—— **自洽，无需修改** |
> | 2 | L1184 指出「`ai_engine.rag.HybridRetriever/Reranker` 仍为 stub，建议由 `rag-core` 统一承接」—— **该判断仍然正确** |
> | 3 | 全文仅 1 处 `Chroma` 提及，且为**对比性讨论**，非选型主张 |
>
> ℹ️ 与 `RAG对照解析.md` 的分工：白皮书讲「**有什么、契约是什么**」，对照解析讲「**代码怎么算出来的**」。
>
> **该信谁**：见 `文件集合/codex/0.施工标准与定义归属.md` **§0.11 文档权威层级**。
> 一句话：**落点查 L0（`文件集合/codex/`）／契约字段查 L1（`RAG_MODULE_DESIGN.md`）／实现与命名查 L2（`RAG搭建框架.md`）／仓库现状查 L3（`项目代码深度阅读与分析报告.md`）**。



> **文档定位**：生产级 RAG 检索与 Tool-calling 模块的**契约基线 + 渐进式实施手册 + 验收标准**。  
> **适用项目**：`contract-guard-ai`（Polyglot Monorepo，`E:/important_project/Serve_bage`）。  
> **版本**：v1.0 · 对齐代码基座 `rag-contracts@1.0.0` / `rag-core` / `cg-observability`（2026-09-08 实测）。  
> **姊妹文档**：`RAG_IMPLEMENTATION_GUIDE.md`（设计侧）、`data_scheme.md`（SSOT 数据方案）、`前端开发指导契约.md`（前端侧）。

---


## 0. 导读：先纠正五个"想当然"（必读）

本项目 RAG 基座**已经实现并跑通**（全量 `99 passed`、`scripts/phase1_smoke.py` 退出码 0）。本白皮书不重造契约，而是**讲透既有契约 + 补齐 Phase 2/3 缺口**。写码前请先更正以下五个常见误判：

| # | 常见假设                             | 真实情况                                                                                                     |
| - | -------------------------------- | -------------------------------------------------------------------------------------------------------- |
| 1 | 检索入口是 `service.query(...)`       | **真名 `RAGService.retrieve(request: RAGQueryRequest) -> RAGQueryResponse`**                               |
| 2 | 切片 schema 叫 `ClauseChunk`        | **真名 `ContractChunk`**（`rag_contracts/chunk.py`，28 字段）                                                   |
| 3 | 召回文档有 `id` / `metadata` 字段       | **是 `chunk_id`，且没有 `metadata`**——元数据全部平铺在顶层，唯一嵌套对象是 `scores` 与 `citation`                                |
| 4 | Phase 2 的 BM25 已有实现              | ❌ **无实现**。`SparseStorePort` 已定义，但 `build_sparse_store()` 恒返回 `None`；`rag-core` 无 `bm25.py`、无 `fusion.py` |
| 5 | Reranker 已接 `bge-reranker-v2-m3` | ❌ 只有 `NoopReranker`（恒等透传）。`rerank_mode=local/http` 会打 warning 后回退 noop                                   |

> **契约命名对照**（你提出的需求名 → 代码中的真名）：
>
> | 需求中的名字                   | 代码真名                                                                         | 落盘位置                                                 |
> | ------------------------ | ---------------------------------------------------------------------------- | ---------------------------------------------------- |
> | `ContractChunkSchema`    | `ContractChunk`                                                              | `packages/rag-contracts/rag_contracts/chunk.py`      |
> | `RAGQueryRequestSchema`  | `RAGQueryRequest`                                                            | `packages/rag-contracts/rag_contracts/query.py`      |
> | `RAGQueryResponseSchema` | `RAGQueryResponse`                                                           | 同上                                                   |
> | `AgentRAGToolContract`   | `AgentRAGToolInput` / `AgentRAGToolResult` + `build_function_calling_spec()` | `packages/rag-contracts/rag_contracts/agent_tool.py` |
>
> 四份 JSON Schema 由 `packages/rag-contracts/scripts/export_schemas.py` 导出到  
> `packages/rag-contracts/rag_contracts/schemas/{contract_chunk,rag_query_request,rag_query_response,agent_rag_tool}.schema.json`。

---

## 一、系统架构与数据流转拓扑（Data Flow Topology）


### 1.1 全链路数据流转图

```
┌─────────────── 离线建库（Offline Indexing） ──────────────────────────────┐
│                                                                            │
│  合同 PDF/DOCX/扫描件          法条 / 标准条款范本（JSONL）                 │
│        │                                │                                 │
│        ▼                                │                                 │
│  doc-processor                          │                                 │
│  (ocr / layout / parser / aligner)      │                                 │
│  → 纯文本 + BBox(PDF_POINTS)            │                                 │
│        │                                │                                 │
│        └──────────────┬─────────────────┘                                 │
│                       ▼                                                    │
│              SourceDoc（dataclass 载体）                                   │
│                       │  SimpleChunker.chunk_many()                        │
│                       │  红线：绝不跨条款/法条边界切                        │
│                       ▼                                                    │
│              ContractChunk ×N（28 字段，chunk_id = uuid5 确定性）           │
│                       │                                                    │
│        ┌──────────────┴──────────────┐                                    │
│        ▼                             ▼                                    │
│  EmbedderPort                  SparseStorePort                            │
│  HashEmbedder(离线) /          BM25Store（★Phase 2 待建）                  │
│  HttpEmbedder(bge-m3→1024d)    Jieba/二元组切词 + pickle 落盘              │
│        │                             │                                    │
│        ▼                             ▼                                    │
│  VectorStorePort               ./data/bm25/index.pkl                      │
│  InMemoryVectorStore                                                       │
│  QdrantVectorStore(1024d/COSINE)                                          │
└────────────────────────────────────────────────────────────────────────────┘

┌─────────────── 在线召回（Online Retrieval） ───────────────────────────────┐
│  Query（待审条款 / 用户问题）                                               │
│        │  RAGService.retrieve(RAGQueryRequest)  ← 唯一 facade 入口          │
│        ├─ ① 超长拦截   len(query) > max_query_chars → OVERLONG_QUERY        │
│        ├─ ② 双路召回   Dense Top-20 (Qdrant)  ‖  Sparse Top-20 (BM25)      │
│        ├─ ③ RRF 融合   score = Σ 1/(k+rank), k=60  →  ★Phase 2 待建        │
│        ├─ ④ 精排重排   RerankerPort（bge-reranker-v2-m3，HTTP）             │
│        │              Top-20 → Top-N(默认 5)                               │
│        ├─ ⑤ 阈值过滤   final < score_threshold(0.35) → 丢弃                │
│        │              ⚠ clamp 而非 (x+1)/2，见 §1.3                        │
│        ├─ ⑥ 溯源映射   to_citation(chunk, score) → CitationOut             │
│        │              （与 PreciseCitation 1:1 的唯一转换点）               │
│        └─ ⑦ 上下文组装 ContextBuilder.build(docs, budget=3000)             │
│                       │                                                    │
│                       ▼                                                    │
│              RAGQueryResponse（含 context_block / degraded / token_usage）  │
└────────────────────────────────────────────────────────────────────────────┘

┌─────────────── Agent 上下文注入（Tool-calling） ───────────────────────────┐
│  LangGraph StateGraph                                                      │
│   router_node → retrieve_node(@tool retrieve_contract_knowledge)           │
│              → generate_node                                               │
│        │                                                                    │
│        ▼  AgentState.retrieved_context = AgentRAGToolResult.context_block   │
│  LLM 生成（标准基线 vs 待审条款）                                           │
│        ▼                                                                    │
│  EnhancedRiskItem{ reasoningSteps[], citations[PreciseCitation] }          │
│        ▼                                                                    │
│  前端溯源高亮（PDF.js Overlay 消费 bbox / pageNo / sectionId）              │
└────────────────────────────────────────────────────────────────────────────┘
```


### 1.2 三级存储边界与分工（关系型 / 向量 / 缓存）

| 存储层         | 技术选型                                                | 真实连接参数                                                                                                       | 存什么                                                                                                                                                                                                                | **不存什么**                                  |
| ----------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------- |
| **关系型数据库**  | PostgreSQL 16（`postgres:16-alpine`）                 | `postgresql+asyncpg://cg:cg@localhost:5432/cg`（容器内主机名 `postgres`）                                            | 合同**元数据**与业务结果：`Contract`(status/version/healthScore/reportId)、`ContractElement`、`EnhancedRiskItem`、`CrossClauseConflict`、`SealSignatureDetection`、`FeedbackItem`、`SystemAuditConfiguration`（可配置规则库）、`raw_docs` 原文 | ❌ 不存向量、❌ 不存切片正文（正文在向量库 payload 与对象存储）     |
| **向量数据库**   | Qdrant（`qdrant/qdrant:latest`，**1024 维 / COSINE**）  | HTTP `http://localhost:6333`，gRPC 6334（容器内 `qdrant:6333`）                                                    | `ContractChunk.to_payload()` 全字段 + 向量；`chunk_id` 即 Qdrant point_id（uuid5 确定性幂等）                                                                                                                                    | ❌ 不做业务事务、❌ 不做权限过滤（过滤靠 payload keyword 索引） |
| **稀疏索引**    | BM25（pickle 落盘）★Phase 2 待建                          | `CG_RAG_BM25_DIR=./data/bm25`                                                                                | 倒排 + TF + 原文（供重排取 text）                                                                                                                                                                                            | ❌ 不存向量                                    |
| **对象存储**    | MinIO                                               | `localhost:9000`（console 9001），`minioadmin/minioadmin`；⚠️ **compose 未自动创建 bucket，需 `mc mb local/contracts`** | 合同源文件、渲染报告 PDF                                                                                                                                                                                                     | ❌ 不存切片/向量                                 |
| **缓存 & 队列** | Redis 7                                             | `redis://localhost:6379/0`；Celery broker `/1`、backend `/2`                                                   | 任务状态机、Celery 队列、`cache_ttl_s=300` 的检索缓存（⚠️ 缓存当前**未实现**）                                                                                                                                                            | ❌ 不作为主存                                   |
| **进程内内存**   | `InMemoryVectorStore` + `TraceContext`(contextvars) | —                                                                                                            | 本地开发/单测的向量、trace_id/span_id 透传                                                                                                                                                                                     | ❌ 不跨进程持久化                                 |

> **边界铁律**：PostgreSQL 是**唯一业务真值源**；Qdrant 是**可重建的派生索引**（`delete_by_doc` + 重建即可恢复）；BM25 是**可重建的派生索引**。任何索引损坏都不得影响业务数据。

### 1.3 三条不可违背的设计红线（源自源码注释，不是我编的）

**红线 1：`dense` 用 clamp 映射，绝不是 `(x+1)/2`**  
`rag_core/service.py` L9-13 原文注释：

> `dense` 保存原始余弦相似度 `[-1,1]`；`final` 为 `[0,1]`。这里用 **clamp 而非 `(x+1)/2`** 映射：后者会把"无关文本"（余弦≈0）抬到 0.5，直接击穿 0.35 阈值，使"缺失标准对照"信号失效。

**后果**：若误用 `(x+1)/2`，赛题要求的"缺失条款检测"会彻底失灵——所有无关条款都被判为"有对照基线"。

**红线 2：`sourceType` 只有三值，禁止新增第四值**  
`STANDARD_CLAUSE`（企业标准条款范本）/ `LAW_ARTICLE`（法律法规条文）/ `PRECEDENT`（裁判文书）。与 SSOT 的 `CitationSourceType`（`openapi.yaml` L130-132）双向对齐。

**红线 3：RAG 内部错误码 `CG-RAG-xxxx` 绝不外泄**  
对外只收敛到 `ErrorCode` 的 13 个成员：超时 → `ERR_LLM_TIMEOUT`，其余 → `ERR_INTERNAL`。  
`ERR_EMPTY_RECALL` **不是错误**，是正常降级信号，走 `degraded=True` 不抛异常。

**附：为什么不新增 RAG HTTP 端点**  
`openapi.yaml` 现有 17 个 path，**无一条 RAG 端点**（tags 仅 Contracts/Agent/Config/Report/Tasks/Feedback）。新增端点会触发 `gen:py`/`gen:ts`/`gen:zod` 三条生成链。因此 RAG 定位为**内部 Python 包 + Agent Tool Calling**，不惊动 SSOT。

---

## 二、核心数据契约（Data Contracts）

> **契约生成**：`packages/rag-contracts/scripts/export_schemas.py` → 4 份 JSON Schema。  
> **校验**：`packages/rag-contracts/tests/test_contracts.py`（26 passed）即契约的可执行证明。


### 2.1 `ContractChunkSchema`（= `ContractChunk`）

**约定**：`model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)`；`EMBEDDING_DIM = 1024`（项目锁定 bge-m3）；`chunk_id` 为 uuid5 确定性 ID（命名空间 `6ba7b811-9dad-11d1-80b4-00c04fd430c8`），兼作 Qdrant point_id。

**JSON Schema（节选核心，`additionalProperties: false`）**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "ContractChunk",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "schema_version", "chunk_id", "doc_id", "source_type", "chunk_type",
    "section_id", "seq_in_doc", "text", "text_hash", "char_count", "token_count"
  ],
  "properties": {
    "schema_version": { "const": "1.0.0" },
    "chunk_id":  { "type": "string", "minLength": 8, "maxLength": 64, "pattern": "^[A-Za-z0-9_\\-\\.]+$" },
    "doc_id":    { "type": "string", "minLength": 1, "maxLength": 200 },
    "source_type": { "enum": ["STANDARD_CLAUSE", "LAW_ARTICLE", "PRECEDENT"] },
    "chunk_type":  { "enum": ["SECTION","CLAUSE","ARTICLE","PARAGRAPH","ITEM","TABLE","WINDOW_SLICE","RECITAL"] },
    "section_id":  { "type": "string", "minLength": 1, "maxLength": 64, "description": "如 '6.2.1' / '第五百八十五条'" },
    "law_code":    { "type": ["string","null"], "maxLength": 64, "description": "source_type=LAW_ARTICLE 时必填" },
    "law_title":   { "type": ["string","null"], "maxLength": 200 },
    "article_no":  { "type": ["string","null"], "maxLength": 64 },
    "parent_section_id": { "type": ["string","null"], "maxLength": 64, "description": "chunk_type=WINDOW_SLICE 时必填" },
    "section_path": { "type": "string", "maxLength": 512, "default": "" },
    "level":       { "type": "integer", "minimum": 0, "maximum": 8, "default": 0, "description": "0文档/1章/2节/3条/4款/5项" },
    "seq_in_doc":  { "type": "integer", "minimum": 0 },
    "title":       { "type": ["string","null"], "maxLength": 200 },
    "text":        { "type": "string", "minLength": 1, "maxLength": 8000, "description": "绝不跨条款边界切" },
    "text_hash":   { "type": "string", "pattern": "^[0-9a-f]{16}$", "description": "sha256(text)[:16]" },
    "char_count":  { "type": "integer", "minimum": 0 },
    "token_count": { "type": "integer", "minimum": 0 },
    "page_no":     { "type": ["integer","null"], "minimum": 1 },
    "bbox":        { "$ref": "#/$defs/BBox" },
    "contract_type": { "enum": ["PURCHASE","SALES","NDA","OUTSOURCING","LABOR","OTHER","null"] },
    "category":    { "type": "string", "maxLength": 64, "default": "UNSPECIFIED" },
    "effective_date": { "type": ["string","null"], "format": "date" },
    "tags":        { "type": "array", "items": { "type": "string" }, "default": [] },
    "embedding_model": { "type": ["string","null"], "maxLength": 100 },
    "embedding_dim":   { "type": ["integer","null"], "minimum": 1, "description": "必须 == 1024" },
    "created_at":  { "type": "string", "format": "date-time" },
    "extra":       { "type": "object", "default": {}, "description": "逃生舱，不入 Qdrant 索引" }
  },
  "$defs": {
    "BBox": {
      "type": "object",
      "additionalProperties": false,
      "required": ["page", "x", "y", "width", "height"],
      "properties": {
        "page": { "type": "integer", "minimum": 1 },
        "x": { "type": "number" }, "y": { "type": "number" },
        "width": { "type": "number" }, "height": { "type": "number" },
        "coordinate_system": { "const": "PDF_POINTS", "default": "PDF_POINTS" }
      }
    }
  }
}
```

**三条 `@model_validator(mode="after")` 硬约束**（`chunk.py` L151-165）

```python
if self.source_type is SourceType.LAW_ARTICLE and not self.law_code:
    raise ValueError("source_type=LAW_ARTICLE 时 law_code 必填")
if self.chunk_type is ChunkType.WINDOW_SLICE and not self.parent_section_id:
    raise ValueError("chunk_type=WINDOW_SLICE 时 parent_section_id 必填")
if self.embedding_dim is not None and self.embedding_dim != EMBEDDING_DIM:  # 1024
    raise ValueError(f"embedding_dim 必须为 {EMBEDDING_DIM}（项目锁定）")
```

**关键方法**

```python
# rag_contracts/chunk.py 中 ContractChunk 的方法签名摘录
# （真实实现含完整 docstring 与逻辑，此处仅展示可被外部调用的接口面）
from typing import Any


class ContractChunk:  # 摘录：仅展示方法签名，真实定义见 rag_contracts/chunk.py
    @staticmethod
    def compute_text_hash(text: str) -> str: ...      # sha256(text)[:16]
    @staticmethod
    def make_chunk_id(doc_id: str, section_id: str, slice_idx: int = 0) -> str: ...  # uuid5 确定性
    def citation_title(self) -> str: ...   # 兜底链：title → law_title+article_no → law_title → section_path → doc_id#section_id
    def to_payload(self) -> dict[str, Any]: ...   # model_dump(mode="json") 并 pop("extra")
    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ContractChunk": ...
```


### 2.2 `RAGQueryRequestSchema` / `RAGQueryResponseSchema`

**请求（`RAGQueryRequest`，`query.py` L43-80）**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "RAGQueryRequest",
  "type": "object",
  "additionalProperties": false,
  "required": ["query"],
  "properties": {
    "trace_id":        { "type": "string", "description": "默认 uuid4 自动生成" },
    "query":           { "type": "string", "minLength": 1, "maxLength": 4000 },
    "top_k":           { "type": "integer", "minimum": 1, "maximum": 50, "default": 8 },
    "score_threshold": { "type": "number", "minimum": 0.0, "maximum": 1.0, "default": 0.35 },
    "strategy":        { "enum": ["DENSE_ONLY","SPARSE_ONLY","HYBRID_RRF"], "default": "HYBRID_RRF" },
    "enable_rerank":   { "type": "boolean", "default": true },
    "rerank_top_n":    { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 },
    "enable_sparse":   { "type": "boolean", "default": true },
    "filters":         { "$ref": "#/$defs/RetrievalFilters" },
    "max_context_tokens": { "type": "integer", "minimum": 256, "maximum": 32000, "default": 3000 },
    "timeout_ms":      { "type": "integer", "minimum": 100, "maximum": 30000, "default": 3000 },
    "with_highlight":  { "type": "boolean", "default": false }
  },
  "$defs": {
    "RetrievalFilters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "source_types":  { "type": ["array","null"], "items": { "enum": ["STANDARD_CLAUSE","LAW_ARTICLE","PRECEDENT"] } },
        "chunk_types":   { "type": ["array","null"], "items": { "type": "string" } },
        "contract_type": { "enum": ["PURCHASE","SALES","NDA","OUTSOURCING","LABOR","OTHER","null"] },
        "law_codes":     { "type": ["array","null"], "items": { "type": "string" } },
        "doc_ids":       { "type": ["array","null"], "items": { "type": "string" } },
        "categories":    { "type": ["array","null"], "items": { "type": "string" } }
      }
    }
  }
}
```

**响应（`RAGQueryResponse`，`query.py` L170-202）**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "RAGQueryResponse",
  "type": "object",
  "additionalProperties": false,
  "required": ["trace_id", "query", "strategy", "retrieved_docs", "token_usage", "schema_version"],
  "properties": {
    "trace_id":          { "type": "string" },
    "query":             { "type": "string" },
    "strategy":          { "enum": ["DENSE_ONLY","SPARSE_ONLY","HYBRID_RRF"] },
    "retrieved_docs":    { "type": "array", "items": { "$ref": "#/$defs/RetrievedDoc" } },
    "total_candidates":  { "type": "integer", "default": 0 },
    "context_block":     { "type": "string", "default": "", "description": "空召回时为 EMPTY_CONTEXT_HINT" },
    "context_tokens":    { "type": "integer", "default": 0 },
    "truncated":         { "type": "boolean", "default": false },
    "degraded":          { "type": "boolean", "default": false, "description": "true=已降级，非错误" },
    "fallback_reason":   { "enum": ["NONE","OVERLONG_QUERY","EMPTY_RECALL","TIMEOUT","STORE_UNAVAILABLE","RERANK_FAILED","EMBED_FAILED"] },
    "warnings":          { "type": "array", "items": { "type": "string" }, "default": [] },
    "token_usage":       { "$ref": "#/$defs/TokenUsage" },
    "stage_latency_ms":  { "type": "object", "additionalProperties": { "type": "integer" } },
    "took_ms":           { "type": "integer", "default": 0 },
    "schema_version":    { "const": "1.0.0" }
  },
  "$defs": {
    "RetrievedDoc": {
      "type": "object",
      "additionalProperties": false,
      "required": ["chunk_id", "doc_id", "source_type", "chunk_type", "section_id", "text", "scores", "citation"],
      "properties": {
        "chunk_id": { "type": "string" },
        "doc_id": { "type": "string" },
        "source_type": { "enum": ["STANDARD_CLAUSE","LAW_ARTICLE","PRECEDENT"] },
        "chunk_type": { "type": "string" },
        "section_id": { "type": "string" },
        "law_code": { "type": ["string","null"] },
        "article_no": { "type": ["string","null"] },
        "title": { "type": ["string","null"] },
        "text": { "type": "string" },
        "highlight": { "type": ["string","null"], "description": "⚠️ 当前恒为 null（未实现）" },
        "page_no": { "type": ["integer","null"] },
        "bbox": { "type": ["object","null"] },
        "contract_type": { "type": ["string","null"] },
        "category": { "type": "string", "default": "UNSPECIFIED" },
        "section_path": { "type": "string", "default": "" },
        "token_count": { "type": "integer", "default": 0 },
        "scores": { "$ref": "#/$defs/ChunkScores" },
        "citation": { "$ref": "#/$defs/CitationOut" }
      }
    },
    "ChunkScores": {
      "type": "object",
      "additionalProperties": false,
      "required": ["final", "rank"],
      "properties": {
        "dense":  { "type": ["number","null"], "description": "原始余弦 [-1,1]" },
        "sparse": { "type": ["number","null"], "description": "BM25 原始分" },
        "fused":  { "type": ["number","null"], "description": "RRF Σ1/(k+rank)；⚠️ 当前恒为 null（Phase 2 待建）" },
        "rerank": { "type": ["number","null"], "minimum": 0.0, "maximum": 1.0 },
        "final":  { "type": "number", "minimum": 0.0, "maximum": 1.0 },
        "rank":   { "type": "integer", "minimum": 1 }
      }
    },
    "CitationOut": {
      "type": "object",
      "additionalProperties": false,
      "required": ["sourceType", "sourceTitle", "contentChunk", "relevanceScore"],
      "properties": {
        "sourceType":     { "enum": ["STANDARD_CLAUSE","LAW_ARTICLE","PRECEDENT"] },
        "sourceTitle":    { "type": "string" },
        "contentChunk":   { "type": "string", "description": "超 800 字截断并追加 '…'" },
        "relevanceScore": { "type": "number", "minimum": 0.0, "maximum": 1.0 }
      },
      "description": "⚠️ 唯一使用 camelCase 的模型，与 SSOT PreciseCitation 1:1 冻结"
    },
    "TokenUsage": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "embedding_tokens": { "type": "integer", "default": 0 },
        "rerank_tokens": { "type": "integer", "default": 0 },
        "prompt_tokens": { "type": "integer", "default": 0 },
        "completion_tokens": { "type": "integer", "default": 0 },
        "total_tokens": { "type": "integer", "default": 0 },
        "estimated": { "type": "boolean", "default": true, "description": "true=估算值（中文 1 token≈1.5 字符）" }
      }
    }
  }
}
```


### 2.3 `AgentRAGToolContract`（Function Calling Standard）

**工具名**：`retrieve_contract_knowledge`（常量 `TOOL_NAME`）。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "AgentRAGToolInput",
  "type": "object",
  "additionalProperties": false,
  "required": ["query"],
  "properties": {
    "query": {
      "type": "string", "minLength": 1, "maxLength": 2000,
      "description": "待检索的条款原文或法务问题"
    },
    "contract_type": { "enum": ["PURCHASE","SALES","NDA","OUTSOURCING","LABOR","OTHER","null"] },
    "source_types": {
      "type": ["array","null"],
      "items": { "enum": ["STANDARD_CLAUSE","LAW_ARTICLE","PRECEDENT"] },
      "description": "限定检索范围；null=不限"
    },
    "top_k": { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 },
    "reason": { "type": ["string","null"], "maxLength": 300, "description": "调用理由，写入审计日志" }
  }
}
```

**出参（`AgentRAGToolResult`）** — `status` 三态是降级契约的核心：

| status     | 含义                         | 触发条件                              | Agent 应如何反应                       |
| ---------- | -------------------------- | --------------------------------- | --------------------------------- |
| `OK`       | 正常召回                       | 有 ≥1 条 `final >= score_threshold` | 正常引用 `citations` 生成结论             |
| `DEGRADED` | 部分能力降级（如 Reranker 失败、策略退化） | `warnings` 非空但仍有结果                | 结论中标注"检索能力降级"                     |
| `EMPTY`    | 空召回                        | `fallback_reason=EMPTY_RECALL`    | 用 `EMPTY_CONTEXT_HINT` 提示"缺失标准对照" |

**`EMPTY_CONTEXT_HINT`（源码常量，必须原样传给 LLM）**

> 知识库未检索到可对照的标准条款或法条，请依据通用法律原则评估，并在结论中明确标注【缺失标准对照】。

**Function Calling Spec 生成**（`agent_tool.py` L126-151）

```python
def build_function_calling_spec() -> dict[str, Any]:
    """用 model_json_schema(mode='serialization') 自动生成，递归内联 $ref，
    设 additionalProperties=False、required=sorted(properties)、strict=True。
    返回 {"type":"function","function":{"name","description","strict","parameters"}}。"""
```

### 2.4 契约红线速查（写码前对照）

| # | 红线                                                                                                                                   | 违反后果              |
| - | ------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| 1 | `sourceType` 三值冻结，与 `PreciseCitation` 1:1                                                                                            | 前端溯源枚举失配、报告缺引用    |
| 2 | `CitationOut` 是**唯一** camelCase 模型，其余全 snake_case                                                                                    | 序列化字段错乱           |
| 3 | 生成层（`generated/schemas.py`）是 snake_case 字段 + camelCase alias 且**无 `model_config`** → 入参必须 camelCase，出参必须 `model_dump(by_alias=True)` | 校验失败 / 前端取不到值     |
| 4 | `EnhancedRiskItem.citations` 为空数组 `[]` 而非 `null`                                                                                     | 前端渲染崩溃            |
| 5 | 对外错误码只能用 `ErrorCode` 13 成员；`CG-RAG-xxxx` 不外泄                                                                                         | 违反 SSOT           |
| 6 | `ERR_EMPTY_RECALL` / `degraded=true` 是**正常降级**，不得抛异常                                                                                 | 无谓告警、链路中断         |
| 7 | `percent` 一律 **0~1**（`TaskProgress`/`ProgressMessage`）；`HealthScore` 反而是 0~100                                                       | 进度条显示错误           |
| 8 | `embedding_dim` 必须 == 1024                                                                                                           | Qdrant upsert 400 |

---

## 三、渐进式开发路线图与可执行代码框架

### 3.0 环境准备（实测结论，Windows）

**已安装可直接用**（零安装）：`fastapi 0.141.1`、`pydantic 2.13.4`、`pydantic_settings`、`sqlalchemy 2.0.52`、`asyncpg`、`redis 6.4.0`、`minio 7.2.20`、`qdrant_client 1.19.0`、`langgraph 1.2.11`、`langchain_core 1.6.0`、`openai 3.3.1`、`pdfplumber 0.11.10`、`python-docx 1.2.0`、`httpx 0.28.1`、`pytest 9.1.1`、`numpy`。  
**缺失**：`celery`、`sentence_transformers`、`jieba`、`rank_bm25`、`chromadb`、`pymupdf`、`structlog`、`loguru`。

> ⚠️ **Windows 上 `PYTHONPATH` 必须用 `;` 分隔**（用 `:` 会导致全部包 `ModuleNotFoundError`）。

**一条命令跑全量测试（实测 `99 passed in 4.75s`）**

```bash
cd E:/important_project/Serve_bage
PYTHONPATH="E:/important_project/Serve_bage/apps/api-gateway;E:/important_project/Serve_bage/packages/rag-contracts;E:/important_project/Serve_bage/packages/cg-observability;E:/important_project/Serve_bage/packages/rag-core" \
E:/important_project/Serve_bage/.venv/Scripts/python.exe -m pytest -q
```

---


### Phase 1：基础 MVP 通路（目标 30 分钟跑通全流程闭环）

> **好消息：Phase 1 已完成且可离线运行**——`scripts/phase1_smoke.py`（331 行）就是现成答案，无需从零写。  
> 它内置 `sys.path` 自举（L28-32），**无需 `pip install`**；用 `HashEmbedder` + `InMemoryVectorStore`，**不需要 API Key / 外网 / GPU**。

**第 1 步：跑通**

```bash
cd E:/important_project/Serve_bage
E:/important_project/Serve_bage/.venv/Scripts/python.exe scripts/phase1_smoke.py
```

**真实输出（实测退出码 0）**

```
  切片数：26
  入库数：26
  查询数：3（空召回 1 条）
  Token 耗用（检索阶段合计）：459
  Token 口径：估算(len/1.5)
```

```
  1  0.4896  STANDARD_CLAUSE  第十条       不可抗力
  2  0.4677  LAW_ARTICLE      第五百九十条  不可抗力
```

> ⚠️ **必须预先告知读者**：3 条查询中 1 条走 `EMPTY_RECALL` 降级，这是 `HashEmbedder` 语义表达力有限的**设计行为，不是故障**（脚本自身已说明）。不解释清楚会被误判为失败。

**第 2 步：理解建库写法（照抄这段，`RAGService` 没有 `index()` 方法）**

```python
# 建库：RAGService 不提供 index()，由调用方手工三步组装
from rag_contracts.ports import VectorRecord

texts = [c.text for c in chunks]
vectors: list[list[float]] = []
for start in range(0, len(texts), batch_size=32):
    vectors.extend(embedder.embed_batch(texts[start:start + 32]))

dim = len(vectors[0]) if vectors else 0
store.ensure_collection(dim)

records = [
    VectorRecord(
        chunk=chunk.model_copy(update={
            "embedding_model": embedder.model_name,
            "embedding_dim": dim,          # 必须 == 1024
        }),
        vector=vector,
    )
    for chunk, vector in zip(chunks, vectors)
]
written = store.upsert(records)
```

**第 3 步：真实调用链（唯一 facade 入口）**

```python
from rag_core.config import RagSettings
from rag_core.factory import build_embedder, build_reranker, build_vector_store
from rag_core.service import RAGService
from rag_core.chunking import SimpleChunker, SourceDoc
from cg_observability import TokenCounter
from rag_contracts.enums import RetrieveStrategy
from rag_contracts.query import RAGQueryRequest

settings = RagSettings()                       # 读 CG_RAG_* 环境变量
counter = TokenCounter()
chunker = SimpleChunker(counter=counter, max_chars=600)

service = RAGService(
    embedder=build_embedder(settings),
    vector_store=build_vector_store(settings),
    counter=counter,
    reranker=build_reranker(settings),
    sparse_store=None,                         # ⚠️ 当前只能 None（无 BM25 实现）
    dense_top=settings.dense_top,
    default_max_context_tokens=settings.max_context_tokens,
)

response = service.retrieve(RAGQueryRequest(   # ★ 方法名是 retrieve，不是 query
    query="买方逾期付款的违约金怎么约定，上限是多少",
    top_k=3,
    strategy=RetrieveStrategy.DENSE_ONLY,      # 显式指定，避开 HYBRID_RRF 自动退化
    score_threshold=settings.score_threshold,  # 0.35
    max_context_tokens=settings.max_context_tokens,
))
```

**第 4 步：日志规范（可观测性——你要的"含 Token 耗用量、检索 Score"）**

统一入口 `log_event(logger, stage, *, level, msg, trace_id, span_id, **fields)`：

```python
from cg_observability import get_logger, log_event, TokenMeter

logger = get_logger("rag_core.service")
meter = TokenMeter(TokenCounter())

with meter.stage("embed"):
    vector = embedder.embed_one(request.query)
meter.count_text("embedding", request.query)

log_event(logger, "retrieve.done",
          trace_id=response.trace_id,
          candidates=response.total_candidates,
          returned=len(response.retrieved_docs),
          top_score=response.retrieved_docs[0].scores.final if response.retrieved_docs else 0.0,
          token_count=response.token_usage.total_tokens,
          context_tokens=response.context_tokens,
          degraded=response.degraded,
          took_ms=response.took_ms)
```

单行 JSON 输出形态（`JsonFormatter`，`ensure_ascii=False` 保留中文）：

```json
{"ts":"2026-08-29T02:11:03.221Z","level":"INFO","logger":"rag_core.service",
 "stage":"retrieve.done","trace_id":"9f1c...","span_id":"b2e1...","msg":"retrieve done",
 "candidates":40,"returned":5,"top_score":0.87,"token_count":1532,"took_ms":218}
```

**标准埋点清单**（切片长度 / 召回得分 / Token 全部覆盖）：

| stage              | 必打字段                                                                                                                   |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `chunk.start`      | `doc_id`, `source_type`, `raw_chars`                                                                                   |
| `chunk.done`       | `chunk_count`, `avg_chars`, `max_chars`, `min_chars`, `over_limit_count`, `avg_tokens`, `token_count`, `invalid_count` |
| `embed.done`       | `model`, `batch_count`, `batch_size`, `dim`, `embed_tokens`, `latency_ms`                                              |
| `upsert.done`      | `store`, `count`, `failed_count`, `latency_ms`                                                                         |
| `retrieve.start`   | `trace_id`, `query_len`, `query_hash`(sha1[:8]), `top_k`, `threshold`, `strategy`, `has_filters`                       |
| `retrieve.dense`   | `candidates`, `top_score`, `latency_ms`                                                                                |
| `retrieve.rerank`  | `model`, `pairs`, `score_max/min/mean`, `dropped_count`, `latency_ms`                                                  |
| `retrieve.degrade` | `fallback_reason`, `detail`, `error_code`(可选)                                                                          |
| `context.build`    | `injected_chunks`, `context_tokens`, `budget`, `truncated`, `prompt_preview`                                           |
| `retrieve.done`    | `candidates`, `returned`, `top_score`, `token_count`, `context_tokens`, `degraded`, `took_ms`                          |

---


### Phase 2：生产级召回率优化（混合检索 BM25 + Reranker）

> **现状**：`SparseStorePort` 已定义（`rag_contracts/ports.py` L112-155），但**无实现**；无 RRF 融合（`ChunkScores.fused` 恒 `None`）；Reranker 只有 `NoopReranker`。  
> **本阶段新建 3 个文件**：`bm25_store.py`、`fusion.py`、`http_reranker.py`。  
> 🎯 **零新增强制依赖**：BM25 复用既有 `rag_core.adapters.embedding.tokenize`（中文二元组切词，无需 jieba）；Reranker 走 HTTP（复用已装的 `httpx`），**规避 `sentence-transformers`/`torch`**（`rag-core` pyproject 明令禁止引入）。

#### 3.2.1 切片策略：层级 / 条款感知切片（已实现，需理解其行为）

`SimpleChunker`（`rag_core/chunking/simple_chunker.py` L120-366）：

```python
_CLAUSE_HEAD_RE = re.compile(r"^\s*(?P<num>第[一二三四五六七八九十百千零〇\d]+[条章节款项]"
                             r"|\d+(?:\.\d+)+|\d+\))\s*(?P<rest>.*)$")
```

| 行为   | 规则                                                                                                                       |
| ---- | ------------------------------------------------------------------------------------------------------------------------ |
| 类型判定 | `source_type == LAW_ARTICLE` → `ChunkType.ARTICLE`；否则 `CLAUSE`                                                           |
| 层级   | `level = 3`（CLAUSE/ARTICLE）、`4`（其余）                                                                                      |
| 超长切分 | `max_chars=600` 触发 `_window_slice()` → `WINDOW_SLICE`，`section_id = f"{section_id}#{slice_idx}"` 且必带 `parent_section_id` |
| 红线   | **绝不跨条款/法条边界切**                                                                                                          |
| 校验失败 | 返回 `None`，计入 `ChunkStats.invalid_count`（不中断）                                                                             |

```python
chunker = SimpleChunker(counter=counter, max_chars=600, min_chars=2, window_overlap=0)
chunks, stats = chunker.chunk_with_stats(doc)   # stats.to_log_fields() 直接喂 log_event
```

#### 3.2.2 BM25 稀疏索引（新建 `packages/rag-core/rag_core/adapters/bm25_store.py`）

```python
"""BM25 稀疏检索：实现 rag_contracts.ports.SparseStorePort。

零新增依赖：复用 rag_core.adapters.embedding.tokenize 做中文二元组切词（无需 jieba）。
磁盘持久化：pickle 单文件；同一 chunk_id 先删后写，保证幂等重跑。
"""
from __future__ import annotations

import math
import pickle
from collections import defaultdict
from pathlib import Path
from typing import Sequence

from rag_contracts.chunk import ContractChunk
from rag_contracts.ports import SparseHit
from rag_core.adapters.embedding import tokenize

_K1 = 1.5
_B = 0.75


class BM25Store:
    """BM25 倒排索引（disk-persisted, idempotent upsert）。"""

    def __init__(self, data_dir: str | Path = "./data/bm25") -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tf: dict[str, dict[str, int]] = {}          # chunk_id -> {term: freq}
        self._texts: dict[str, str] = {}                  # chunk_id -> 原文（供重排）
        self._doc_len: dict[str, int] = {}                # chunk_id -> token 数
        self._doc_source: dict[str, str] = {}             # chunk_id -> doc_id
        self._df: defaultdict[str, int] = defaultdict(int)
        self._avgdl = 0.0
        self._load()

    # ---------- 持久化 ----------
    @property
    def _path(self) -> Path:
        return self._dir / "index.pkl"

    def _load(self) -> None:
        if self._path.exists():
            blob = pickle.loads(self._path.read_bytes())
            (self._tf, self._texts, self._doc_len,
             self._doc_source, self._df, self._avgdl) = blob

    def save(self) -> None:
        self._path.write_bytes(pickle.dumps(
            (self._tf, self._texts, self._doc_len,
             self._doc_source, dict(self._df), self._avgdl)))

    # ---------- SparseStorePort ----------
    def upsert(self, chunks: Sequence[ContractChunk]) -> int:
        """幂等写入：同一 chunk_id 先删旧的再写入。"""
        for c in chunks:
            self._remove(c.chunk_id)
            tf: defaultdict[str, int] = defaultdict(int)
            for t in tokenize(c.text):
                tf[t] += 1
            self._tf[c.chunk_id] = dict(tf)
            self._texts[c.chunk_id] = c.text
            self._doc_len[c.chunk_id] = sum(tf.values())
            self._doc_source[c.chunk_id] = c.doc_id
            for t in tf:
                self._df[t] += 1
        total = sum(self._doc_len.values())
        self._avgdl = (total / len(self._doc_len)) if self._doc_len else 0.0
        self.save()
        return len(list(chunks))

    def search(self, query: str, top_k: int = 20) -> list[SparseHit]:
        """BM25 打分；text 一并返回，供下游 Cross-Encoder 重排（否则重排文本为空）。"""
        if not self._tf:
            return []
        q_tokens = tokenize(query)
        n = len(self._doc_len)
        avgdl = self._avgdl or 1.0
        scores: dict[str, float] = {}
        for qt in q_tokens:
            df = self._df.get(qt, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            if idf <= 0:
                continue
            for cid, tf in self._tf.items():
                f = tf.get(qt, 0)
                if f == 0:
                    continue
                dl = self._doc_len[cid]
                denom = f + _K1 * (1 - _B + _B * dl / avgdl)
                scores[cid] = scores.get(cid, 0.0) + idf * f * (_K1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [SparseHit(chunk_id=cid, score=float(s),
                          text=self._texts.get(cid, "")) for cid, s in ranked]

    def delete_by_doc(self, doc_id: str) -> int:
        targets = [cid for cid, src in self._doc_source.items() if src == doc_id]
        for cid in targets:
            self._remove(cid)
        self.save()
        return len(targets)

    def health(self) -> bool:
        return True

    # ---------- 内部 ----------
    def _remove(self, chunk_id: str) -> None:
        if chunk_id not in self._tf:
            return
        for t in self._tf[chunk_id]:
            self._df[t] = max(0, self._df[t] - 1)
        self._tf.pop(chunk_id, None)
        self._texts.pop(chunk_id, None)
        self._doc_len.pop(chunk_id, None)
        self._doc_source.pop(chunk_id, None)
```

> **为什么自带实现而不装 `rank_bm25`**：① 复用既有 `tokenize`，零新增依赖；② 需要 `delete_by_doc` 与幂等 upsert，`rank_bm25` 是无状态内存库不提供；③ 需要 pickle 落盘与 `text` 回传。  
> 若坚持用 `rank_bm25`：`uv pip install --python .venv/Scripts/python.exe rank_bm25`（jieba 可选，`tokenize` 已覆盖中文二元组）。

#### 3.2.3 RRF 融合（新建 `packages/rag-core/rag_core/fusion.py`）

```python
"""Reciprocal Rank Fusion：score(d) = Σ_lists 1/(k + rank_l(d))，k=60。

与 ChunkScores.fused 对应 —— 当前 service 中 fused 恒为 None，本函数在 Phase 2 接入后填充。
"""
from __future__ import annotations

from typing import Sequence

DEFAULT_RRF_K = 60


def rrf_fuse(
    ranked_lists: Sequence[Sequence[tuple[str, float]]],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[str, float]]:
    """把多路有序结果（(chunk_id, score)）融合为单一有序列表。

    入参: ranked_lists —— 每路已是按分数降序的 (chunk_id, score) 序列。
    出参: 按 RRF 分降序的 [(chunk_id, fused_score), ...]（fused 为 0~1 的小数，非归一）。
    边界: 空输入返回 []；单路输入等价于保留该路顺序。
    """
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (chunk_id, _score) in enumerate(ranked, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: item[1], reverse=True)
```

**在 `RAGService._recall()` 中接入（改动点，`service.py` L270-324）**

```python
# 伪位置：_recall() 内，dense 与 sparse 都拿到之后
if strategy == RetrieveStrategy.HYBRID_RRF and self._sparse_store is not None:
    sparse_hits = self._sparse_store.search(request.query, top_k=self._dense_top)
    fused = rrf_fuse(
        [(h.chunk_id, h.score) for h in dense_hits],
        [(h.chunk_id, h.score) for h in sparse_hits],
        k=rrf_k,                                  # CG_RAG_RRF_K，默认 60
    )
    # 用 fused 顺序重排候选，并把 fused 分回填 ChunkScores.fused
```

#### 3.2.4 精排重排器（新建 `packages/rag-core/rag_core/adapters/http_reranker.py`）

> 本地 CrossEncoder 需 `torch`，被 `rag-core` 设计硬约束禁止 → **走 HTTP 重排服务**（TEI / vLLM / 自建 rerank）。

```python
"""HTTP Cross-Encoder 重排：实现 rag_contracts.ports.RerankerPort。

设计约束：rag-core 禁止引入 sentence-transformers / torch，故本地模型一律走 HTTP 服务。
失败语义：HTTP 异常/超时 → 抛 RerankFailedError（retryable），由 RAGService._rerank_safe
降级为 base_scores 并置 fallback_reason=RERANK_FAILED（不中断检索）。
"""
from __future__ import annotations

import time
from typing import Sequence

import httpx

from rag_contracts.errors import RerankFailedError


class HttpReranker:
    def __init__(
        self,
        url: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        *,
        api_key: str = "none",
        timeout_ms: int = 3000,
        query_path: str = "/rerank",
    ) -> None:
        if not url:
            raise RerankFailedError("rerank http url 未配置", detail={"url": url})
        self._url = url.rstrip("/")
        self._model = model
        self._timeout_s = max(0.1, timeout_ms / 1000.0)
        self._path = query_path
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key != "none" else {}

    @property
    def model_name(self) -> str:
        return self._model

    def rerank(
        self,
        query: str,
        texts: Sequence[str],
        base_scores: Sequence[float] | None = None,
    ) -> list[float]:
        """对 (query, text) 逐对打分，返回与 texts 等长的 [0,1] 分数列表。

        出参保证：len(返回) == len(texts)；任一项失败则整体抛 RerankFailedError 触发降级。
        """
        if not texts:
            return []
        payload = {"model": self._model, "query": query, "texts": list(texts)}
        started = time.perf_counter()
        try:
            resp = httpx.post(
                f"{self._url}{self._path}", json=payload,
                headers=self._headers, timeout=self._timeout_s,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:                       # 网络/超时/非 2xx 一律降级
            raise RerankFailedError(
                "rerank 服务调用失败",
                detail={"model": self._model,
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "error": str(exc)},
            ) from exc

        raw = body.get("scores") if isinstance(body, dict) else None
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise RerankFailedError(
                "rerank 返回结构不合法",
                detail={"expected": len(texts), "got": (len(raw) if isinstance(raw, list) else None)},
            )
        # clamp 到 [0,1]：与 service 红线一致，禁止 (x+1)/2
        return [max(0.0, min(1.0, float(s))) for s in raw]
```

**工厂接入**（`rag_core/factory.py` `build_reranker`，替换现有"回退 noop + warning"分支）

```python
# 替换 rag_core/factory.py 中 build_reranker 现有"回退 noop + warning"分支
from rag_contracts.ports import RerankerPort
from rag_core.adapters.noop_rerank import NoopReranker
from rag_core.adapters.http_reranker import HttpReranker
from rag_core.config import RagSettings


def build_reranker(settings: RagSettings) -> RerankerPort | None:
    """按配置装配重排器；http 模式失败时由 RAGService 降级，不在此处抛异常。"""
    if not settings.rerank_enabled:
        return None
    if settings.rerank_mode == "http":
        return HttpReranker(
            url=settings.rerank_http_url,
            model=settings.rerank_model,
            api_key=settings.embed_api_key or "none",   # 复用 embedding 的 key
            timeout_ms=settings.embed_timeout_ms,
        )
    # local 需 torch（被 rag-core 依赖硬约束禁止）→ 一律 noop
    return NoopReranker()
```

**启用 Phase 2**

```bash
CG_RAG_STRATEGY=HYBRID_RRF CG_RAG_RERANK_MODE=http \
CG_RAG_RERANK_HTTP_URL=http://localhost:8081 \
E:/important_project/Serve_bage/.venv/Scripts/python.exe scripts/phase1_smoke.py
```



> ⚠️ **注意默认值不一致**：`RAGQueryRequest.strategy` 默认 `HYBRID_RRF`，而 `RagSettings.strategy` 默认 `DENSE_ONLY`。且 `strategy != DENSE_ONLY and sparse_store is None` 时，service 会自动降级为 `DENSE_ONLY` 并追加 warning `"未接入 BM25（Phase 2），策略已退化为 DENSE_ONLY"`。接入 BM25 后此 warning 消失是 Phase 2 的验收信号之一。

---


### Phase 3：Agent Tool-calling 深度整合

> **现状**：契约侧已就绪（`AgentRAGToolInput/Result` + `build_function_calling_spec()`），但**无 `@tool` 装饰的 LangGraph 绑定层**。本阶段新建 `packages/ai-engine/ai_engine/agents/rag_tool.py`。  
> `langgraph 1.2.11` + `langchain_core 1.6.0` 已安装，零安装成本。

#### 3.3.1 工具封装（含超长拦截 / 空召回降级 / 超时重试）

```python
"""把 RAGService 封装为 LangGraph / LangChain 标准工具。

边界思维三件套（必须全部具备）：
  1. 超长文本拦截：query 超过 max_query_chars → 截断，fallback_reason=OVERLONG_QUERY
  2. 空召回降级  ：无命中 → status=EMPTY + EMPTY_CONTEXT_HINT，绝不抛异常
  3. 超时重试    ：TransientRagError(retryable=True) 指数退避重试，耗尽后走降级
"""
from __future__ import annotations

import time
from typing import Any

from langchain_core.tools import tool

from cg_observability import get_logger, log_event
from rag_contracts.agent_tool import (
    EMPTY_CONTEXT_HINT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    AgentRAGToolResult,
)
from rag_contracts.enums import FallbackReason
from rag_contracts.errors import RagError, TransientRagError
from rag_contracts.query import RAGQueryRequest, TokenUsage

_LOGGER = get_logger("ai_engine.agents.rag_tool")
_DEFAULT_MAX_QUERY_CHARS = 2000
_DEFAULT_MAX_RETRIES = 3


def build_rag_tool(
    service: Any,
    *,
    max_query_chars: int = _DEFAULT_MAX_QUERY_CHARS,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    default_top_k: int = 5,
) -> Any:
    """构造 retrieve_contract_knowledge 工具。

    入参: service —— RAGService 实例（有 .retrieve(request) -> RAGQueryResponse）
    出参: 被 @tool 装饰的可调用对象，返回值是 AgentRAGToolResult.to_tool_message() 字符串
    """

    @tool(TOOL_NAME, description=TOOL_DESCRIPTION)
    def retrieve_contract_knowledge(
        query: str,
        contract_type: str | None = None,
        top_k: int = default_top_k,
    ) -> str:
        """从企业标准条款库、《民法典》等法律法规与裁判文书中检索相关条款。

        query: 待审条款原文或法务问题（超长会被截断）
        contract_type: PURCHASE/SALES/NDA/OUTSOURCING/LABOR/OTHER，可选过滤
        top_k: 返回条数，1~20
        """
        warnings: list[str] = []
        fallback = FallbackReason.NONE
        started = time.perf_counter()

        # ---- 边界 1：超长文本拦截 ----
        if len(query) > max_query_chars:
            query = query[:max_query_chars]
            warnings.append(f"query 超过 {max_query_chars} 字已截断")
            fallback = FallbackReason.OVERLONG_QUERY
            log_event(_LOGGER, "rag_tool.truncate", level="WARNING",
                      max_query_chars=max_query_chars)

        # ---- 边界 3：超时重试（仅对 retryable 的瞬时错误）----
        response = None
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                response = service.retrieve(RAGQueryRequest(
                    query=query,
                    top_k=top_k,
                    filters=_build_filters(contract_type),
                ))
                break
            except TransientRagError as exc:            # retryable=True
                last_error = exc
                if attempt == max_retries:
                    break
                time.sleep(0.1 * (2 ** (attempt - 1)))  # 指数退避 0.1/0.2/0.4s
            except RagError as exc:                     # 不可重试
                last_error = exc
                break

        # ---- 重试耗尽 / 非瞬时错误 → 降级为空上下文，绝不向上抛 ----
        if response is None:
            code = getattr(last_error, "code", "CG-RAG-0000")
            fallback = FallbackReason.TIMEOUT if code == "CG-RAG-1004" else FallbackReason.STORE_UNAVAILABLE
            warnings.append(f"检索失败已降级：{code}")
            log_event(_LOGGER, "rag_tool.degrade", level="ERROR",
                      fallback_reason=fallback.value, error_code=code)
            return AgentRAGToolResult(
                status="DEGRADED",
                context_block=EMPTY_CONTEXT_HINT,
                citations=[], trace_id="", took_ms=_elapsed_ms(started),
                token_usage=TokenUsage(), warnings=warnings,
                fallback_reason=fallback,
            ).to_tool_message()

        # ---- 边界 2：空召回降级 ----
        docs = response.retrieved_docs
        if not docs:
            fallback = FallbackReason.EMPTY_RECALL
            warnings.append("知识库无命中，已注入 EMPTY_CONTEXT_HINT")
            log_event(_LOGGER, "rag_tool.empty", level="WARNING",
                      trace_id=response.trace_id, fallback_reason=fallback.value)
            return AgentRAGToolResult(
                status="EMPTY",
                context_block=EMPTY_CONTEXT_HINT,
                citations=[], trace_id=response.trace_id,
                took_ms=_elapsed_ms(started),
                token_usage=response.token_usage,
                warnings=warnings + list(response.warnings),
                fallback_reason=fallback,
            ).to_tool_message()

        status = "DEGRADED" if (response.degraded or warnings) else "OK"
        log_event(_LOGGER, "rag_tool.done", trace_id=response.trace_id,
                  returned=len(docs), top_score=docs[0].scores.final,
                  degraded=response.degraded, took_ms=response.took_ms, status=status)
        return AgentRAGToolResult(
            status=status,
            context_block=response.context_block,
            citations=[d.citation for d in docs],
            trace_id=response.trace_id,
            took_ms=response.took_ms or _elapsed_ms(started),
            token_usage=response.token_usage,
            warnings=warnings + list(response.warnings),
            fallback_reason=response.fallback_reason if response.degraded else fallback,
        ).to_tool_message()

    return retrieve_contract_knowledge


def _build_filters(contract_type: str | None) -> Any:
    from rag_contracts.enums import ContractType
    from rag_contracts.query import RetrievalFilters
    if not contract_type:
        return RetrievalFilters()
    try:
        return RetrievalFilters(contract_type=ContractType(contract_type))
    except ValueError:
        return RetrievalFilters()          # 非法枚举值不阻断，退化为不过滤


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
```

#### 3.3.2 LangGraph 图编排（router → retrieve → generate）

```python
"""合同审核 Agent 图：条件边决定是否检索，retrieve_node 注入 retrieved_context。"""
from __future__ import annotations

from typing import Annotated, TypedDict

import operator
from langgraph.graph import StateGraph, END


class AgentState(TypedDict, total=False):
    query: str                                    # 待审条款 / 用户问题
    contract_type: str | None
    intent: str                                   # CHITCHAT | RISK_DIAGNOSIS | CLAUSE_REWRITE
    retrieved_context: str                        # ★ retrieve_node 写入
    citations: list                               # CitationOut 列表，供报告溯源
    messages: Annotated[list, operator.add]
    answer: str


def build_contract_agent(rag_tool, llm_call) -> StateGraph:
    def router_node(state: AgentState) -> dict:
        intent = "CHITCHAT" if len(state.get("query", "")) < 8 else "RISK_DIAGNOSIS"
        return {"intent": intent}

    def retrieve_node(state: AgentState) -> dict:
        ctx = rag_tool.invoke({
            "query": state["query"],
            "contract_type": state.get("contract_type"),
            "top_k": 5,
        })
        return {"retrieved_context": ctx}

    def generate_node(state: AgentState) -> dict:
        prompt = (
            "你是企业法务审核助手。\n\n"
            f"【检索到的标准基线】\n{state.get('retrieved_context', '')}\n\n"
            f"【待审条款】\n{state['query']}\n\n"
            "请输出：风险等级(HIGH/MEDIUM/LOW)、理由、修改建议、引用来源。"
        )
        return {"answer": llm_call(prompt)}

    g = StateGraph(AgentState)
    g.add_node("router", router_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("generate", generate_node)
    g.set_entry_point("router")
    g.add_conditional_edges(
        "router",
        lambda s: "end" if s.get("intent") == "CHITCHAT" else "retrieve",
        {"end": END, "retrieve": "retrieve"},
    )
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()
```

> **与既有代码的衔接**：`apps/async-worker/worker/tasks/run_audit.py` L13 已 `from ai_engine.rag import HybridRetriever, Reranker`——Phase 3 完成后应改为注入 `rag_core` 的 `RAGService`（`ai_engine.rag` 里的 `HybridRetriever`/`Reranker` 仍是 `NotImplementedError` 的旧 stub，本白皮书建议**由 `rag-core` 统一承接，避免两套实现**）。

---

## 四、阶段性结果验收标准（Acceptance Criteria）

| 阶段                         | 测试用例（Test Cases）                                                                                                                                                                                                                                 | 预期输出（Expected Output）                                                                                                                                                         | 量化指标（Metrics）                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Phase 1**  
基础 MVP       | ① 运行 `python scripts/phase1_smoke.py`（默认 `--top-k 3`）  
② 语料：`scripts/data/phase1_sample.jsonl`（3 篇：STD-PURCHASE-001 / STD-LABOR-001 / LAW-MINFA-001）  
③ 3 条 DEMO_QUERIES：违约金上限 / 试用期最长 / 不可抗力免责                                                | 控制台打印：`切片数：26`、`入库数：26`、`查询数：3（空召回 1 条）`、`Token 耗用（检索阶段合计）：459`、`Token 口径：估算(len/1.5)`  
结果表格形如 `1  0.4896  STANDARD_CLAUSE  第十条 不可抗力`  
退出码 **0**                            | 建库耗时 **< 5s**；单次检索 **< 300ms**（inmemory + hash）；非空召回 **2/3**（第 3 条 `EMPTY_RECALL` 为设计行为）；`context_tokens ≤ 3000`；检索阶段 Token **≤ 500**                            |
| **Phase 1**  
回归           | `pytest` 全量（见 §3.0 命令，注意 Windows 用 `;`）                                                                                                                                                                                                          | `99 passed`（api-gateway 31 + rag-contracts 26 + cg-observability 14 + rag-core 28）                                                                                            | 通过率 **100%**；耗时 < 10s                                                                                                                                            |
| **Phase 2**  
混合检索         | ① 同语料 + 新增 10 条查询集（覆盖 5 类合同 × 法条）  
② `CG_RAG_STRATEGY=HYBRID_RRF` + BM25 接入  
③ 故意停用 rerank 服务，验证降级                                                                                                                                             | 日志新增 `retrieve.sparse` 与 `retrieve.fusion`；`ChunkScores.fused` **非 None**；warning **不再出现**"未接入 BM25"；rerank 失败时 `degraded=true` + `fallback_reason=RERANK_FAILED` 且**仍有结果返回** | **Recall@5 ≥ 0.80**；**Recall@10 ≥ 0.90**；p95 延迟 **≤ 800ms**（inmemory）/ **≤ 1500ms**（Qdrant + HTTP rerank）；`context_tokens ≤ 3000`；`top_score ≥ 0.35` 的查询占比 ≥ 80% |
| **Phase 2**  
契约校验         | 构造非法 `ContractChunk`：① `LAW_ARTICLE` 无 `law_code` ② `WINDOW_SLICE` 无 `parent_section_id` ③ `embedding_dim=768`                                                                                                                                   | 三条均抛 `ValueError`（`ContractValidationError` / `CG-RAG-3001`），且**不写入** Qdrant                                                                                                  | 非法数据拦截率 **100%**；`ChunkStats.invalid_count` 正确计数                                                                                                                 |
| **Phase 3**  
Tool-calling | ① `build_function_calling_spec()` 输出快照断言  
② 正常 query → 期望 `status=OK`  
③ 超长 query（> 2000 字）→ 期望截断 + `OVERLONG_QUERY`  
④ 知识库清空 → 期望 `status=EMPTY` + `EMPTY_CONTEXT_HINT`  
⑤ rerank 服务 500 → 期望 `status=DEGRADED` + `RERANK_FAILED` 且**不抛异常** | Tool 返回串形如 `[status=OK][trace_id=xxx]\n[1] (STANDARD_CLAUSE) 第六条 逾期付款违约金 [section=第六条]\n…`  
⑤ 场景 `citations` 可为空但 `context_block == EMPTY_CONTEXT_HINT`                      | 工具调用成功率（非降级）**≥ 95%**；降级路径覆盖 **100%**（3 条异常路径全部走到）；单次 tool 调用 **≤ 1500ms**；重试次数 **≤ 3**                                                                          |
| **Phase 3**  
端到端          | LangGraph `router → retrieve → generate` 跑 3 条待审条款                                                                                                                                                                                               | `AgentState.retrieved_context` 非空；`answer` 含风险等级 + 修改建议；`citations` 可映射为 `EnhancedRiskItem.citations`（`PreciseCitation`）                                                      | 引用可溯源率 **100%**（每条 citation 的 `sourceTitle`/`contentChunk` 非空）；`relevanceScore ∈ [0,1]`                                                                          |

### 4.1 关键阈值口径说明

- **Recall@k**：`命中 gold chunk_id 的条数 / gold 总数`，gold 由人工标注的标准条款 `section_id` 确定。取 k=5 为主指标（对应赛题"风险识别召回 ≥ 75%"，RAG 侧设 0.80 为更严门禁）。
- **score_threshold = 0.35**：既是"有对照基线"的判定线，也是"缺失条款检测"的触发线。`final < 0.35` → 该条不返回；全部不返回 → `EMPTY_RECALL` → 报告输出"缺失标准对照，建议补全"。
- **Token 上限**：`max_context_tokens = 3000`（`CG_RAG_MAX_CONTEXT_TOKENS`），`ContextBuilder.build()` 至少注入第 1 条，超出则 `truncated=true`。Token 口径默认**估算**（中文 1 token ≈ 1.5 字符，`TokenCounter.estimated=True`）；装 `tiktoken` 后自动切换精确计数。
- **延迟口径**：`stage_latency_ms` 分阶段（embed / dense / sparse / rerank / build），`took_ms` 为总耗时。用 `TokenMeter.stage()` 上下文管理器采集。

---

## 五、已知缺口与后续工单（T03 待建清单）

| 缺口               | 现状                                                               | 本白皮书对策                                         |
| ---------------- | ---------------------------------------------------------------- | ---------------------------------------------- |
| BM25 稀疏检索        | `build_sparse_store()` 恒 `None`                                  | §3.2.2 提供 `BM25Store` 完整实现                     |
| RRF 融合           | 无 `fusion.py`，`fused` 恒 `None`                                   | §3.2.3 提供 `rrf_fuse()` + 接入点                   |
| 真实 Reranker      | 仅 `NoopReranker`；local/http 回退 noop                              | §3.2.4 提供 `HttpReranker`                       |
| LangGraph 绑定     | 无 `@tool` 层                                                      | §3.3.1 提供 `build_rag_tool()`                   |
| 两套检索实现并存         | `ai_engine.rag.HybridRetriever/Reranker` 仍为 stub，与 `rag-core` 重复 | 建议由 `rag-core` 统一承接，废弃 `ai_engine.rag` stub    |
| `with_highlight` | 字段存在但无人赋值，恒 `None`                                               | 前端如需高亮，Phase 2 后在 `_rank()` 中赋值                |
| 缓存 / 重试          | `cache_ttl_s`、`max_retries` 有配置无实现                               | §3.3.1 已实现重试；缓存建议 Redis + `trace_id` 维度        |
| `celery` 未安装     | worker 侧任务无法导入                                                   | `uv pip install celery[redis]`（Phase 3 端到端才需要） |
| MinIO bucket 未创建 | compose 无 `mc mb` 步骤                                             | 首次启动后执行 `mc mb local/contracts`                |

---

## 附录 A：配置速查（`CG_RAG_*`，出自 `.env.example` + `rag_core/config.py`）

```bash
CG_RAG_VECTOR_STORE=inmemory          # inmemory | qdrant
CG_RAG_QDRANT_MODE=local_memory       # server | local_memory | local_path（默认无需起服务）
CG_RAG_QDRANT_URL=http://localhost:6333
CG_RAG_COLLECTION=contract_clauses
CG_RAG_VECTOR_DIM=1024                # 锁定，ContractChunk 强校验
CG_RAG_EMBED_MODE=hash                # hash（离线）| http（bge-m3）
CG_RAG_EMBED_BASE_URL=http://localhost:8080
CG_RAG_EMBED_MODEL=BAAI/bge-m3
CG_RAG_EMBED_BATCH_SIZE=32
CG_RAG_BM25_DIR=./data/bm25
CG_RAG_SPARSE_TOP=20
CG_RAG_DENSE_TOP=20
CG_RAG_RRF_K=60
CG_RAG_RERANK_ENABLED=true
CG_RAG_RERANK_MODE=noop               # noop | http（local 需 torch，被禁）
CG_RAG_RERANK_MODEL=BAAI/bge-reranker-v2-m3
CG_RAG_RERANK_THRESHOLD=0.35
CG_RAG_RERANK_TOP_N=5
CG_RAG_MAX_CONTEXT_TOKENS=3000
CG_RAG_MAX_QUERY_CHARS=2000
CG_RAG_TIMEOUT_MS=3000
CG_RAG_MAX_RETRIES=3
CG_RAG_SCORE_THRESHOLD=0.35
CG_RAG_STRATEGY=DENSE_ONLY            # ⚠️ 与 RAGQueryRequest 默认 HYBRID_RRF 不一致
CG_RAG_LOG_LEVEL=INFO
```

> `CG_RAG_*` 是**环境变量**（经 `RagSettings` 读取），**不是 CLI flag**。`phase1_smoke.py` 真正的 CLI flag 只有 `--corpus` 与 `--top-k`。

## 附录 B：基础设施连接（宿主机本地）

```
PostgreSQL : postgresql+asyncpg://cg:cg@localhost:5432/cg
Redis      : redis://localhost:6379/0   （Celery broker /1，backend /2）
Qdrant     : http://localhost:6333      （gRPC 6334）
MinIO      : localhost:9000             （console 9001，minioadmin/minioadmin，bucket 需自建）
```

容器内主机名分别为 `postgres` / `redis` / `qdrant` / `minio`。启动：`docker compose -f docker/docker-compose.yml up -d`。
