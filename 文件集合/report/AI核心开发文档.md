# AI 核心开发文档（教学级落地指南）

> **配套文档**：`核心构建PRD.md`（选型与里程碑）、`data_scheme.md`（数据类型契约）、`architecture_assembly_report_2026-08-18.md`（Monorepo 骨架）
> **目标读者**：直接参与本系统开发的工程师（后端 / AI 引擎 / 前端）
> **本文定位**：把 PRD 的"选型决策"翻译为"逐行可执行步骤"，覆盖 **OCR → RAG → Agent → 三者联动 → 输出版本管理** 全链路，每一步都给出代码骨架、落点文件与验证方法。

---

## 0. 阅读前必读：现有 Infra 与代码落点

本系统采用 **Polyglot Monorepo**（根：`Serve_bage/contract-guard-ai`）。AI 核心相关落点：

| 你想改的东西 | 落点目录 | 本文对应章节 |
|---|---|---|
| OCR / 版面 / 解析 | `packages/doc-processor/{ocr,layout,parser,aligner}` | §1 |
| 知识库清洗 | `packages/rag-etl/{extractors,transformers,pipelines}` | §2 |
| 向量检索 / 重排 | `packages/ai-engine/rag/{retriever,reranker}` | §2 |
| LLM 接入 | `packages/ai-engine/llm/` | §3.1 |
| Agent 编排 | `packages/ai-engine/graphs/` | §3 |
| 分层记忆 | `packages/ai-engine/memory/` | §3.5 |
| 前端 | `apps/web-ui/src` | §4/§5（联动说明） |

**环境基线**（PRD §6.1）：Python 3.11 + `uv`；Node 20 + `pnpm`；服务 `postgres / qdrant / redis / minio`（docker-compose 一键起）。所有对外数据类型以 `data_scheme.md` 为唯一真理源（SSOT）。

**开发纪律（来自 data_scheme §6）**：
- 所有 API 类型由 `packages/schema/openapi.yaml` 生成（`pnpm gen:ts/gen:zod/gen:py`），**禁止手写漂移**。
- 前端请求只走 `@/api/client`；响应必经 Zod 断言。
- ID 一律 `string`、金额 `Money`、百分比 `[0,1]`、时间 ISO8601 UTC、集合空为 `[]`、业务错误用 `code` 枚举。

---

## 1. OCR 引擎搭建（PaddleOCR 直接接入）

> 赛题要求：支持 .docx/.pdf/图片（含 OCR）；保留段落结构。创新点：扫描件印章/签名定位与合规审查。

### 阶段 1.1：安装与最小识别（先跑通一条线）

落点：`packages/doc-processor/ocr/adapter.py`

```bash
cd packages/doc-processor
uv add paddleocr paddlepaddle   # CPU 版；如需 GPU 用 paddlepaddle-gpu
```

```python
# packages/doc-processor/ocr/adapter.py
from paddleocr import PaddleOCR
from pathlib import Path
from typing import List, Dict, Any

class OcrAdapter:
    """封装 GitHub 优质 OCR 项目 PaddleOCR，对外只暴露标准化接口。"""
    def __init__(self, lang: str = "ch", use_gpu: bool = False):
        # use_angle_cls 处理旋转文本；lang=ch 中文最优
        self.engine = PaddleOCR(use_angle_cls=True, lang=lang, use_gpu=use_gpu)

    def recognize_image(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        # 1) 落临时文件（PaddleOCR 接受路径）；生产环境可改用 numpy 直接传
        tmp = Path("/tmp") / f"ocr_{abs(hash(image_bytes))}.png"
        tmp.write_bytes(image_bytes)
        # 2) 返回结构: [ [bbox, (text, score)], ... ] 每层一个 page
        raw = self.engine.ocr(str(tmp), cls=True)
        # 3) 归一为标准行结构
        lines: List[Dict[str, Any]] = []
        for page_idx, page in enumerate(raw or []):
            for bbox, (text, score) in page:
                xs = [p[0] for p in bbox]; ys = [p[1] for p in bbox]
                lines.append({
                    "page": page_idx + 1,
                    "text": text,
                    "confidence": round(float(score), 4),
                    # 见 data_scheme §1.3 BBox：PDF_POINTS，72dpi 左上原点
                    "bbox": {"page": page_idx + 1, "x": min(xs), "y": min(ys),
                             "width": max(xs) - min(xs), "height": max(ys) - min(ys),
                             "coordinateSystem": "PDF_POINTS"},
                })
        return lines
```

**验证**：准备一张含中文的合同截图，调用 `recognize_image` 打印 `lines`，确认 `text` 与 `bbox` 正确。

### 阶段 1.2：原生 PDF 直抽（不走 OCR，更准更快）

落点：`packages/doc-processor/parser/pdf_parser.py`

```python
import pdfplumber
from typing import List, Dict

def parse_native_pdf(path: str) -> List[Dict]:
    """数字版 PDF：直接取文本+坐标，避免 OCR 误差。"""
    out = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(
                x_tolerance=1.5, y_tolerance=1.5,
                extra_attrs=["size", "fontname"])
            for w in words:
                out.append({
                    "page": i,
                    "text": w["text"],
                    "bbox": {"page": i, "x": w["x0"], "y": w["top"],
                             "width": w["x1"] - w["x0"], "height": w["bottom"] - w["top"],
                             "coordinateSystem": "PDF_POINTS"},
                })
    return out
```

> **决策**（PRD §4.1）：数字 PDF 走 `pdfplumber`，扫描件/图片走 PaddleOCR；二者输出统一为 `(text, bbox, page)`，下游无感。

### 阶段 1.3：版面层级还原（PP-StructureV2）

落点：`packages/doc-processor/layout/structure.py`

目的：把"一行行文字"还原为"标题/段落/表格/条款编号"层级，供后续 AST 构建。

```python
from paddleocr import PPStructure

class LayoutAnalyzer:
    def __init__(self):
        # recovery=True 输出带结构的 HTML/JSON；layout 模型含 title/text/table/seal 等区域
        self.engine = PPStructure(recovery=False, layout=True)

    def analyze(self, img_path: str):
        return self.engine(img_path)  # 含 region 类型与坐标
```

把版面区域聚合为条款树（`packages/doc-processor/parser/ast_builder.py`）：依据"第X条""X.X.X"编号与缩进，构造 `ClauseNode{id, number, title, text, bbox, children}`（对应 data_scheme §9 `ClauseNode`）。

### 阶段 1.4：印章与签名定位 + 合规审查（创新点）

落点：`packages/doc-processor/ocr/seal_detector.py`

PaddleOCR / PP-Structure 的 `seal` 类可定位印章区域；再用 OCR 识别印章内文字，做合规判断。

```python
from dataclasses import asdict
from data_scheme import SealSignatureDetection, SealType, SealStatus  # 由 SSOT 生成

class SealDetector:
    def detect(self, img_path: str, parties: List[str]) -> List[SealSignatureDetection]:
        # 1) 印章区域检测（复用 PP-Structure seal 类或专用检测头）
        regions = self._detect_seal_regions(img_path)
        results = []
        for idx, r in enumerate(regions):
            text = self._ocr_inner(r.crop)            # 印章内公司名/税号
            matched = any(p in (text or "") for p in parties)
            status = SealStatus.VALID if (text and matched) else SealStatus.UNAUTHORIZED
            results.append(SealSignatureDetection(
                id=f"seal_{idx}", type=SealType.OFFICIAL_SEAL, status=status,
                bbox=r.bbox, page=r.page, extractedText=text,
                isMatchedWithParties=matched,
                complianceNote="印章名称与合同甲方一致" if matched else "印章名称与双方均不匹配，疑似未授权",
                confidence=r.score))
        return results
```

> 输出直接复用 `data_scheme.md §8.1` 的 `SealSignatureDetection` 类型，前端 `/audit` 左栏 Canvas 据此画闪烁高亮框。

### 阶段 1.5：OCR 与 RAG 的联动（关键）

OCR/解析产物 = **合同全文（带坐标）+ AST + 印章**。它们如何喂给 RAG 与 Agent：

1. **全文文本** → 写入 `Contract.fullText`，同时作为 Agent `extract`/`risk` 节点的输入上下文。
2. **AST 条款切片** → 既作为 Agent 风险定位的"条款粒度"，也作为 RAG 入库的一种 chunk（见 §2.2）。
3. **坐标 BBox** → 贯穿全程，保证右栏风险卡片可"定位"到左栏原文（data_scheme §5.1 联动）。

```python
# packages/doc-processor/pipeline.py（编排）
def process_document(file_bytes, file_type, parties):
    if file_type in ("PDF_NATIVE",):
        lines = parse_native_pdf(buf_to_path(file_bytes))
    else:  # PDF_SCANNED / IMAGE
        lines = OcrAdapter().recognize_image(file_bytes)
    ast = AstBuilder().build(lines)
    seals = SealDetector().detect(buf_to_path(file_bytes), parties) if file_type != "DOCX" else []
    return {"fullText": " ".join(l["text"] for l in lines), "ast": ast, "seals": seals, "lines": lines}
```

---

## 2. RAG 系统搭建（切片 / 索引 / 重排 / 召回优化）

> 赛题要求：标准条款比对、缺失检测、跨条款矛盾、可解释（RAG 溯源）。PRD §4.4 选定「传统 RAG + LlamaIndex 编排 + 自建 ELT + Qdrant」。

### 阶段 2.1：知识库构建（ELT 的 E+L）

落点：`packages/rag-etl/extractors/`

知识源三类：**企业标准条款范本**（Word）、**法律法规**（《民法典》等）、**裁判文书**。

```python
# packages/rag-etl/extractors/standard_clause.py
import docx
def extract_standard_clauses(docx_path: str):
    """解析企业标准 Word 范本，拆为标准条款 Baseline。"""
    d = docx.Document(docx_path)
    clauses = []
    for p in d.paragraphs:
        if p.text.strip():
            clauses.append({"title": p.style.name, "text": p.text.strip(),
                            "source": "STANDARD_CLAUSE", "sourceTitle": docx_path})
    return clauses
```

写入 staging：`packages/rag-etl/pipelines/load.py` 把原始文档落 `postgres.raw_docs`（ELT 的 L）。

### 阶段 2.2：切片策略（Chunking）—— 决定召回上限

落点：`packages/rag-etl/transformers/chunker.py`

**不要用固定字数硬切**，合同语义以"条款"为单位。三种策略组合：

```python
class ClauseAwareChunker:
    def chunk(self, doc) -> List[Dict]:
        chunks = []
        # 策略A：按 AST 条款切片（最优，保留条款边界）
        for node in doc["ast"]:
            chunks.append(self._make_chunk(node.text, node.bbox,
                            meta={"clauseId": node.id, "level": node.level}))
        # 策略B：语义滑动窗（长段落防截断），overlap=0.15
        for node in doc["ast"]:
            if len(node.text) > 800:
                chunks += self._sliding_window(node.text, size=500, overlap=75,
                                               meta={"clauseId": node.id})
        # 策略C：标题+正文聚合（保证上下文完整）
        return chunks

    def _sliding_window(self, text, size, overlap, meta):
        step = size - overlap
        return [self._make_chunk(text[i:i+size], None, meta)
                for i in range(0, len(text), step) if text[i:i+size].strip()]
```

切片要点（经验）：
- 合同类**优先按条款切**，chunk 控制在 300–800 字。
- 必须带 `meta`（条款号、页面、bbox），供 RAG 溯源（`PreciseCitation`）。
- overlap 防边界语义丢失，但不要过大（浪费向量）。

### 阶段 2.3：向量化（Embedding）

落点：`packages/ai-engine/llm/embedding.py`（复用云端 API 适配层）

```python
class EmbeddingClient:
    def __init__(self, provider, api_key, base_url, model):
        self.model = model  # 如 BAAI/bge-large-zh-v1.5 或 云端 embedding
    def embed(self, texts: List[str]) -> List[List[float]]:
        # 走 OpenAI-compatible embeddings 端点（DeepSeek/Qwen/DashScope 均支持）
        ...
```

> 中文合同首选 **bge-large-zh / bge-m3**；若走云端则用对应 embedding 模型，批量 32 条/次。

### 阶段 2.4：索引与存储（Qdrant）

落点：`packages/async-worker/worker/storage/qdrant_client.py`

```python
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

class ClauseVectorStore:
    def __init__(self, host="qdrant", port=6333):
        self.c = QdrantClient(host=host, port=port)

    def ensure_collection(self, name="contract_kb", dim=1024):
        if not self.c.collection_exists(name):
            self.c.create_collection(name, vectors_config=VectorParams(
                size=dim, distance=Distance.COSINE))

    def upsert(self, name, chunks):
        pts = [PointStruct(id=c["id"], vector=c["vector"],
                           payload={"text": c["text"], **c["meta"]}) for c in chunks]
        self.c.upsert(name, pts)

    def search(self, name, vector, top_k=10, filter_=None):
        return self.c.search(name, vector, limit=top_k, query_filter=filter_)
```

### 阶段 2.5：混合检索（Dense + Sparse）

落点：`packages/ai-engine/rag/retriever.py`

纯向量检索对"精确法条编号/金额"不敏感，需 **BM25（关键词）互补**。

```python
from llama_index.core import VectorStoreIndex, Settings
from llama_index.retrievers.bm25 import BM25Retriever

class HybridRetriever:
    def __init__(self, qdrant, bm25_nodes):
        self.dense = qdrant
        self.sparse = BM25Retriever.from_defaults(nodes=bm25_nodes, similarity_top_k=5)
    def retrieve(self, query_vec, query_text, top_k=8):
        d = self.dense.search("contract_kb", query_vec, top_k=top_k)
        s = self.sparse.retrieve(query_text)
        # 倒数排名融合 (RRF)
        fused = self._rrf(d, s, k=60)
        return fused[:top_k]
```

### 阶段 2.6：重排（Reranker，Cross-Encoder）

落点：`packages/ai-engine/rag/reranker.py`

召回 Top-20 后，用 Cross-Encoder 精排到 Top-5，显著提升"相关条款"命中。

```python
from sentence_transformers import CrossEncoder

class Reranker:
    def __init__(self, model_name="BAAI/bge-reranker-v2-m3"):
        self.model = CrossEncoder(model_name)
    def rerank(self, query, candidates, top_n=5):
        pairs = [(query, c["payload"]["text"]) for c in candidates]
        scores = self.model.predict(pairs)
        ranked = sorted(zip(candidates, scores), key=lambda x: -x[1])
        return [c for c, _ in ranked[:top_n]]
```

### 阶段 2.7：召回率优化（可量化、可迭代）

| 手段 | 做法 | 预期收益 |
|---|---|---|
| 查询扩展 | 用 LLM 把"违约金过高"扩展为"违约金/赔偿金/滞纳金 比例 上限" | +长尾召回 |
| 混合检索 | Dense+BM25+RRF（§2.5） | 抗关键词/语义两类查询 |
| 重排 | Cross-Encoder Top-20→5（§2.6） | 精准度↑ |
| 元数据过滤 | 按 `contractType`/`sourceType` 过滤 | 降噪 |
| 评估闭环 | 用标注集算 Recall@5，每周回归 | 防退化 |
| 阈值调参 | 相似度 < 0.35 判"缺失"（缺失检测） | 降误报 |

**评估脚本**（`packages/rag-etl/pipelines/eval_recall.py`）：取 50 条标准条款为 query，人工标注相关 chunk，计算 Recall@5 / MRR，纳入 CI 门禁。

### 阶段 2.8：LlamaIndex 编排（把上面串起来）

```python
Settings.embed_model = embed_client
Settings.llm = llm_adapter.to_llama()
index = VectorStoreIndex.from_vector_store(qdrant_store)
query_engine = index.as_query_engine(retriever=hybrid_retriever, node_postprocessors=[reranker])
```

---

## 3. Agent 系统搭建（LangGraph）

> PRD §4.3：LangGraph 状态图 + Checkpointer + 分层 Memory + human-in-the-loop。赛题创新点：多轮对话修改、CoT 透明、可解释。

### 阶段 3.1：LLM 适配层（云端 API）

落点：`packages/ai-engine/llm/adapter.py`

```python
from openai import OpenAI  # OpenAI-compatible，DeepSeek/Qwen/OpenAI 通用

class LLMAdapter:
    def __init__(self, provider: str, api_key: str, base_url: str, model: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
    def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        r = self.client.chat.completions.create(
            model=self.model, temperature=temperature,
            messages=[{"role":"system","content":system},
                      {"role":"user","content":user}])
        return r.choices[0].message.content
```

> 多模型兜底：在 `config.py` 配置主/备 provider，异常捕获后切换（PRD §4.2）。Key 仅服务端环境变量，不入前端。

### 阶段 3.2：状态图设计（核心）

落点：`packages/ai-engine/graphs/audit_graph.py`

```python
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated
import operator

class AuditState(TypedDict):
    contract_id: str
    full_text: str
    ast: list
    contract_type: str
    elements: list          # 要素抽取结果
    risks: list             # EnhancedRiskItem
    conflicts: list         # 跨条款矛盾
    missing_clauses: list
    report: dict
    # 累加器：多个节点都会往里追加
    notes: Annotated[list, operator.add]

def build_audit_graph():
    g = StateGraph(AuditState)
    g.add_node("classify", classify_node)
    g.add_node("extract", extract_node)
    g.add_node("risk_detect", risk_node)
    g.add_node("conflict_detect", conflict_node)
    g.add_node("standard_compare", compare_node)
    g.add_node("report_draft", report_node)
    g.set_entry_point("classify")
    g.add_edge("classify", "extract")
    g.add_edge("extract", "risk_detect")
    g.add_edge("risk_detect", "conflict_detect")
    g.add_edge("conflict_detect", "standard_compare")
    g.add_edge("standard_compare", "report_draft")
    g.add_edge("report_draft", END)
    # 低置信分支可加条件边 → 人工复核（human-in-the-loop）
    return g.compile(checkpointer=PostgresSaver.from_conn_string(DB_URL))
```

### 阶段 3.3：节点实现（以 risk_detect 为例，含 CoT + 溯源）

落点：`packages/ai-engine/agents/auditor.py`

```python
from data_scheme import EnhancedRiskItem, RiskLevel, ReasoningStep, PreciseCitation

def risk_node(state: AuditState):
    retrieved = retriever.retrieve(state["full_text"])   # §2 混合检索
    system = load_prompt("risk_audit", role="严苛法务审计员")  # PromptRoleConfig
    user = f"合同全文：{state['full_text']}\n参考标准/法条：{retrieved}\n请输出 JSON"
    raw = llm.chat(system, user, temperature=0.2)
    parsed = json.loads(raw)
    risks: List[EnhancedRiskItem] = []
    for r in parsed["risks"]:
        risks.append(EnhancedRiskItem(
            id=r["id"], clauseId=r["clauseId"], category=r["category"],
            level=RiskLevel(r["level"]), title=r["title"],
            originalText=r["originalText"], bbox=r["bbox"],
            suggestion=r["suggestion"], legalBasis=r.get("legalBasis"),
            # ★ CoT 透明化：把模型逐步推理落库
            reasoningSteps=[ReasoningStep(stepNumber=i+1, thought=s["thought"],
                            evidenceText=s["evidenceText"])
                            for i, s in enumerate(r["reasoningSteps"])],
            # ★ RAG 精确溯源：每条风险挂法条/标准条款引用
            citations=[PreciseCitation(sourceType=c["sourceType"],
                        sourceTitle=c["sourceTitle"], contentChunk=c["contentChunk"],
                        relevanceScore=c["relevanceScore"]) for c in r["citations"]],
        ))
    return {"risks": risks, "notes": ["risk_done"]}
```

> 强制要求（data_scheme §8.4）：`reasoningSteps` 与 `citations` **不可省略**，前端 Audit 页据此渲染"推理链"与"溯源卡片"，满足可解释性评分点。

### 阶段 3.4：多轮对话机制（Rewrite Agent）

落点：`packages/ai-engine/graphs/rewrite_graph.py`

用户在前端点 [AI重写] 输入"改成对等解约"，需要**多轮**且**不超载**。

```python
def build_rewrite_graph():
    g = StateGraph(RewriteState)
    g.add_node("load_context", load_scoped_context)  # 只载入该条款局部
    g.add_node("generate_diff", generate_diff_node)
    g.add_node("apply_diff", apply_diff_node)
    g.set_entry_point("load_context")
    g.add_edge("load_context", "generate_diff")
    g.add_edge("generate_diff", "apply_diff")  # apply 后等待用户下一轮（断点续跑）
    return g.compile(checkpointer=PostgresSaver(...))
```

多轮靠 `thread_id`（LangGraph Checkpointer）恢复；用户每轮 `instruction` 追加到 `ScopedClauseMemory`，生成新 `RewriteResponse{before,after,diff,reasoningSteps,citations}`（data_scheme §8.4）。

### 阶段 3.5：记忆系统（分层 Memory，防上下文超载）

落点：`packages/ai-engine/memory/`

```python
# global_state.py —— 全局合同状态（Postgres，跨会话稳定）
class GlobalContractState:
    def load(self, contract_id): ...   # 元数据+AST 拓扑+currentVersionId
    def save_version(self, contract_id, new_ast): ...  # 生成 v1.1

# scoped_memory.py —— 条款级记忆（Redis，仅 3-5 轮滑动窗口）
class ScopedClauseMemory:
    def __init__(self, redis, ttl=1800): self.r = redis
    def append(self, clause_id, role, content):
        key = f"mem:clause:{clause_id}"
        self.r.rpush(key, json.dumps({"role":role,"content":content}))
        self.r.ltrim(key, -5, -1)     # 只保留最近 5 轮
        self.r.expire(key, self.ttl)
    def get(self, clause_id): return [json.loads(x) for x in self.r.lrange(...)]
```

三层（对应 PRD §4.3 / data_scheme §10）：
- **Global**：合同元数据 + AST + 当前版本。
- **Scoped**：仅当前条款近 3–5 轮，避免全量重载。
- **RAG Context**：每次请求按需注入标准条款 + 法条（检索即得，不入长记忆）。

### 阶段 3.6：Tool Calling（让 Agent 自己调工具）

落点：`packages/ai-engine/agents/tools.py`

把"检索标准条款""比对金额""查法条"封装为工具，LLM 自主决定调用（而非每次全量注入）。

```python
tools = [
  {
    "type": "function",
    "function": {
      "name": "search_standard_clause",
      "description": "检索企业标准条款库，输入条款主题，返回相关标准条款与来源",
      "parameters": {"type":"object","properties":{"topic":{"type":"string"}},"required":["topic"]}
    }
  },
  {
    "type": "function",
    "function": {
      "name": "search_law",
      "description": "检索《民法典》等法律法规，返回法条原文与出处",
      "parameters": {"type":"object","properties":{"keyword":{"type":"string"}},"required":["keyword"]}
    }
  },
]
# 调用：llm.chat with tools=tools，解析 tool_calls 执行后回填
```

### 阶段 3.7：CoT 透明推理与输出样式规范化

- **CoT**：要求模型以结构化 JSON 输出 `reasoningSteps`（步骤+原句证据），不只用自然语言糊弄（§3.3）。
- **输出样式**：所有 Agent 输出收敛到 data_scheme 类型——`EnhancedRiskItem`/`RewriteResponse`/`CrossClauseConflict`/`SealSignatureDetection`，**绝不返回未声明字段**。
- **Diff 样式**：改写返回 `before/after` + `diff`（见 data_scheme §2.4 `AuditMessage` 的 `DIFF` 类型），前端渲染红绿对比。

---

## 4. OCR + RAG + Agent 三者联动（端到端时序）

```
用户上传 → presign 直传 MinIO → POST /upload 触发 Celery 任务
  └─ parse_doc 任务:
        OCR/PDF解析 (§1) → AST + 全文 + 印章
        → 写 Postgres(Contract) + 推送 SSE 进度
  └─ run_audit 任务:
        LangGraph audit_graph.invoke (§3.2)
          classify → extract → risk_detect(§3.3, 调 §2 检索+§3.6 tool)
          → conflict_detect → standard_compare(§2 缺失检测) → report_draft
        → 写 risks/conflicts/missing_clauses + citations
  └─ 前端 /audit 渲染:
        左栏 PDF.js 绑 BBox；右栏风险卡片(含 CoT+溯源)
        点 [AI重写] → rewrite_graph 多轮 (§3.4) → 返回 Diff
        点 [采纳] → commit → 新版本 (§5)
  └─ 反馈 → FeedbackItem 飞轮 (审核通过纳入规则库/Prompt Example)
```

**联动要点**：
- OCR 产出的 **bbox** 让 RAG/Agent 的定位能回流到原文高亮。
- RAG 检索结果以 **citations** 形式进入 Agent 输出，形成可解释闭环。
- Agent 的 **tool_calls** 复用 RAG retriever，避免上下文膨胀。

---

## 5. 输出内容版本管理

> 赛题要求：报告与回填内容需版本控制、可回溯。data_scheme §9 `Contract.version` + `currentVersionId`。

### 5.1 版本模型

每次用户"采纳修改"即生成新版本：

```python
# packages/ai-engine/memory/global_state.py
def commit_revision(contract_id, new_ast, changed_clause_ids):
    cur = Contract.objects.get(id=contract_id)
    new_version = cur.version + 1
    Revision.objects.create(contract_id=contract_id, version=new_version,
                            ast=new_ast, changed_clause_ids=changed_clause_ids,
                            parent_version=cur.version)
    cur.version = new_version
    cur.current_version_id = revision.id
    cur.save()
    return new_version   # v1.0 -> v1.1
```

### 5.2 回溯与对照

- 前端 `/audit` 顶部"版本: v1.1 ▼"下拉可切换历史版本，左栏原文与右栏风险点随之重建。
- 比对两版本 AST 差异，渲染"本次改动"Diff（复用 `AuditMessage.DIFF`）。
- 乐观锁：`commit` 必须带回 `version`，冲突抛 `ERR_DATA_CONFLICT`（data_scheme §4.3），前端弹"覆盖/重新拉取"。

### 5.3 报告版本绑定

报告（`/report/:id`）绑定 `currentVersionId`，导出 PDF 时标注版本号与时间戳；历史版本报告可重新生成。

---

## 6. 对照赛题要求：本文如何逐一满足

| 赛题要求 | 本文落点 |
|---|---|
| 分类准确率≥85% | §3.3 classify 节点 + 标准模板先验 + few-shot |
| 要素 F1≥80%（双方/日期/金额/期限/管辖） | §3.3 extract 节点，输出 `ContractElement`（含 bbox/confidence） |
| 风险≥10 类，召回&精准≥75% | §3.3 risk_node + 规则库双路，输出 `EnhancedRiskItem` |
| 标准比对 + 缺失检测 | §2.2/§2.7 + §3.3 standard_compare（RAG 相似度判缺失） |
| 报告含原文位置/等级/建议 | data_scheme `EnhancedRiskItem` + §5 报告版本 |
| Web 界面 + 上传/查看/导出/标注 | §4 前端 5 页 + §5 + FeedbackItem 飞轮 |
| 创新：多轮对话修改 | §3.4 rewrite_graph |
| 创新：知识图谱跨条款 | §3.3 conflict_detect（跨条款矛盾 `CrossClauseConflict`） |
| 创新：印章签名合规 | §1.4 SealDetector → `SealSignatureDetection` |
| 可解释 | §3.3 CoT `reasoningSteps` + §2 溯源 `citations` |
| 可配置 | data_scheme §8.3 `SystemAuditConfiguration`（Prompt/规则/要素） |

---

## 7. 调试、评估与上线

1. **OCR 调试**：准备 3 类样本（数字 PDF / 扫描件 / 图片），核对 `bbox` 与文本；印章样本验证 `SealStatus`。
2. **RAG 评估**：`eval_recall.py` 算 Recall@5；阈值回归纳入 CI。
3. **Agent 评估**：用 CUAD / 脱敏样例算分类准确率、要素 F1、风险召回/精准（PRD §6.5）。
4. **联调**：基于 `openapi.yaml` 动态 Mock（Prism），每接口 ≥2 组 JSON（成功/异常）。
5. **低配部署**：`docker-compose up`；OCR CPU 推理、LLM 云端 API 无需 GPU；`deploy/low-spec.md` 给 2C4G 单机指引。

---

## 8. 上手 Checklist（按此顺序开发）

- [ ] §1.1 跑通 PaddleOCR 单图识别
- [ ] §1.2 数字 PDF 直抽 + §1.3 版面 AST
- [ ] §1.4 印章检测输出 `SealSignatureDetection`
- [ ] §2.1–2.4 知识库入库 Qdrant（含 meta）
- [ ] §2.5–2.6 混合检索 + 重排
- [ ] §2.7 召回评估脚本
- [ ] §3.1 LLM 适配层（云端 API 通）
- [ ] §3.2–3.3 audit_graph + risk_node（带 CoT/溯源）
- [ ] §3.4–3.5 rewrite_graph + 分层 Memory
- [ ] §3.6 Tool Calling
- [ ] §4 端到端联调（Celery 任务 + SSE）
- [ ] §5 版本提交与回溯
- [ ] §6/§7 评估达标 + 低配部署

> 每一步都对应 PRD 的 Phase 0–8 里程碑与 data_scheme 的 SSOT 类型；严格按"先 YAML → 生成类型 → 写实现 → Zod 断言"的顺序，可最大限度避免前后端联调撕裂。
