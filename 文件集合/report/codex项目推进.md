# Codex 项目推进方案 · RAG / Agent / OCR 三线并进

> 文档版本：v1.0　日期：2026-09-20
> 适用项目：`E:\important_project\Serve_bage`（Contract-Guard-AI · 赛题 A24）
> 前置阅读：**`文件集合/report/项目代码深度阅读与分析报告.md`**（本文所有「现状」判断均以该文的 `[实测]`/`[实读]` 结论为准）
> 本文定位：**施工图 + 技术指导**。每个任务都给出「落点文件 / 做什么 / 验收 / 依赖」，可直接据此开 PR。

---

## 0. 先读这一节：本方案的基本判断

### 0.1 三条线的性质完全不同，不要用同一套方法推

| 线 | 性质 | 主矛盾 | 策略 |
|---|---|---|---|
| **RAG** | **加固 + 换引擎** | 契约与编排已完备，缺「语义」与「Milvus 适配」 | **最小改动**：加 1 个适配器，不动业务代码 |
| **Agent** | **从零实现** | 依赖已装好但一行没用过，Graph 拓扑未定 | **先定图，再填节点**：把拓扑定死再写节点 |
| **OCR** | **选型 + 适配** | 输出契约已冻结，缺引擎 | **先用成熟项目跑通，再谈优化** |

### 0.2 一条贯穿全局的工程原则（本项目已为你铺好）

本项目所有「会变的实现」都已经抽象成 **Protocol 端口 + DI 工厂**：

| 要换的东西 | 端口 | 工厂函数 | 新增实现只需 |
|---|---|---|---|
| 向量库（→ Milvus） | `rag_contracts.ports.VectorStorePort` | `factory.build_vector_store()` | 1 个类 + 1 个 `if` 分支 |
| OCR 引擎 | `doc_processor.ocr.OcrEngine`（隐式契约） | 直接 new | 1 个类 |
| 精排模型 | `RerankerPort` | `factory.build_reranker()` | 已支持 `http` 模式 |
| 向量模型 | `EmbedderPort` | `factory.build_embedder()` | 已支持 `http` 模式 |

> ✅ **所以本方案的核心心法是：永远不要改 `service.py`（编排）与 `rag_contracts`（契约），
> 只在 `adapters/` 下加文件、在 `factory.py` 加分支。** 违反这一条会立刻摧毁本项目最大的架构优势。

### 0.3 建议的执行顺序（关键路径）

```
第 1 周  ┃ T-A0 前置修复 ──┬──> T-C1/C2/C3 OCR 跑通单图 ──> T-C7 印章 ──> T-C8 接线
         ┃                  │
         ┃                  └──> T-A1/A2/A3 Milvus 适配器 ──> T-A4 集成测试
         ┃
第 2 周  ┃ T-A5/T-A6 语义服务（embedding + rerank）──> T-A8 评测闭环
         ┃                  │
         ┃                  └──> T-B1 LLM 适配层 ──> T-B2/B3 audit_graph
         ┃
第 3 周  ┃ T-B4 RAG Tool + 三道防线 ──> T-B5 rewrite_graph ──> T-B8 worker 接线
         ┃                  │
         ┃                  └──> T-C4/C5/C6 版面 + 条款 AST + 坐标
         ┃
第 4 周  ┃ 端到端联调 + 前端 PDF 高亮（T-D1） + 评测达标 + 报告导出
```

---

## 1. 决策先行：4 个必须拍板的技术决策

> ⚠️ 这 4 条不定，后面写代码会返工。**建议你按 D1→D4 顺序逐条确认**，确认结论直接写在本文对应位置即可。

### D1【已拍板 2026-09-21】向量库：**Qdrant 主力 + Milvus 备选**

> ✅ **决策已拍板（2026-09-21）**：**维持 Qdrant 为生产/默认向量库，Milvus 降为「备选适配器」**，
> 用途是**展示 Ports & Adapters 的可扩展性**。
>
> | 项 | 结论 |
> |---|---|
> | 主向量库 | **Qdrant**（保持现行配置，不改默认路径） |
> | 离线/单测 | **InMemoryVectorStore**（零依赖，红线不变） |
> | Milvus 定位 | **备选适配器**，P2，不阻塞达标（见 `文件集合/codex/1-2Milvus适配器.md`） |
> | 数据迁移 | **不做**（生产库不切换）→ `T-A10` 取消 |
>
> 🔴 **与下方 D1.1–D1.6 的关系**：下面保留了**完整的原始论证**（含 Milvus 的落地路径与代价），
> 现在它的定位变为 **「备选方案的技术储备 + 可扩展性验收依据」**，而**不是**生产切换计划。
> 其中 **D1.5（用 Milvus 原生稀疏向量替掉自研 BM25）本次明确不做**。
>
> 终裁理由与红线清单见 `文件集合/codex/1.RAG模块.md` **§1.1.3 / §1.7.1**。


#### D1.1 现有设计文档与你的需求冲突，先把冲突摆平

`文件集合/report/RAG_MODULE_DESIGN.md` §1.3 **明确论证过并否决了 Milvus**，原话是：

> 「换 Milvus = 重写索引建表、payload schema、过滤 DSL、运维体系，**代价 ≥ 2 人周且无业务收益**」
> 「**结论建议**：Phase 3 不切 Milvus。若未来确有亿级向量需求，再按上表执行」

**我需要诚实地告诉你这个冲突，因为它是你项目的既有决策记录。** 但结论要**分两半看**：

| 旧文档的判断 | 本次复核结论 | 依据 |
|---|---|---|
| 「换 Milvus 要重写索引建表 / payload schema / 过滤 DSL」 | ⚠️ **当时对，现在明显高估了** | 因为 `VectorStorePort` 已经落地，业务层 `service.py` **一行都不用改**；新增适配器的实际体量 ≈ 250~320 行 + 25 行装配 |
| 「运维体系成本」 | ✅ **这条仍然成立，是真实代价** | Milvus 需要 **etcd + 对象存储（MinIO）+ Milvus** 三件套；而 Qdrant 是**单容器** |

**所以我的技术判断是：代码代价 ~1.5 人日（可接受），运维代价是主要成本（需你确认可接受）。**

#### D1.2 一个必须先验证的硬约束（Windows）

> 🔴 **Milvus Lite（`MilvusClient("./milvus.db")` 本地模式）官方支持矩阵以 Linux / macOS 为主，Windows 原生支持长期缺失。**
> 你的开发机是 Windows（`E:\` + PowerShell）。

**影响**：你**不能**像 Qdrant 那样用「本地文件模式」在 Windows 上零依赖跑 Milvus。

**对策（三选一，按推荐度排序）**：

| 方案 | 做法 | 评价 |
|---|---|---|
| **① Docker standalone（推荐）** | `docker compose` 起 `etcd + minio + milvus`，宿主通过 `localhost:19530` 连接 | ✅ 与生产同构；❌ 需要 Docker 常驻 |
| ② WSL2 内跑 Milvus Lite | WSL2 Ubuntu 内 `pip install pymilvus` 后用 `./milvus.db` | ✅ 开发轻量；❌ 跨文件系统访问慢，与宿主 Python 解释器分离 |
| ③ 混合（**实际最推荐**） | **开发/单测用 `InMemoryVectorStore`（零依赖，已存在）；集成测试与演示用 Docker Milvus** | ✅ 两边都不别扭；这一条也符合本仓「默认路径必须离线可跑」的既有约定 |

> **务必在动手前用 10 分钟验证**：`docker compose` 起 Milvus 三件套 → `pymilvus` 能连上 → `client.list_collections()` 返回正常。
> 若这一步过不去，整套方案要退回方案 ③ 或维持 Qdrant。

#### D1.3 推荐落地形态（**方案 A + 方案 C 的组合**）

```
生产 / 演示  : CG_RAG_VECTOR_STORE=milvus   → MilvusVectorStore(server)      ← 本次要新增的
集成测试     : CG_RAG_VECTOR_STORE=milvus   → MilvusVectorStore(server, 独立 collection)
单测 / 离线  : CG_RAG_VECTOR_STORE=inmemory → InMemoryVectorStore            ← 已存在，保持不动
历史 / 回退  : CG_RAG_VECTOR_STORE=qdrant   → QdrantVectorStore              ← 保留不删，作对照
```

**保留 Qdrant 适配器的三个理由**：
1. 它是**双跑校验的基准**（迁移正确性验证需要它）
2. 它是**回退路径**（Milvus 出问题时可秒切回来）
3. 它**不增加任何维护成本**（已经是测试覆盖的稳定代码）

#### D1.4 技术细节：Milvus 与既有契约的字段映射（**这是最关键的技术指导**）

Milvus 与 Qdrant 的模型差异，必须在适配器内部消化掉，**绝不能让这个差异漏到契约层**：

| 契约概念 | Qdrant 做法（现有） | Milvus 做法（新增） | 注意事项 |
|---|---|---|---|
| `chunk_id`（主键） | `PointStruct.id = chunk_id`（字符串） | `DataType.VARCHAR(is_primary=True, max_length=64)` | ✅ `chunk_id` 是 uuid5（36 字符），64 足够；⚠️ **Milvus VARCHAR 主键必须显式给 `max_length`** |
| `ContractChunk` 全字段 | `payload=chunk.to_payload()`（任意 JSON） | 拆成 **1 个 JSON 字段 `payload`** + **6 个扁平标量字段** | ⚠️ **这是 Milvus 最大的坑**：Milvus 的 JSON 字段**不能高效过滤嵌套键**，而 `RetrievalFilters` 要按 `source_type` / `chunk_type` / `contract_type` / `doc_id` / `law_code` / `category` 过滤 → **必须把这 6 个字段冗余成顶层标量列** |
| 过滤条件 | `models.Filter(must=[FieldCondition(...)])` | `expr` 字符串 DSL，如 `source_type in ["LAW_ARTICLE"]` | ⚠️ **必须做转义**：字符串值里的 `"` 要转义，否则 expr 注入/语法错 |
| 相似度 | `point.score` | `hit["distance"]` | ⚠️ **Milvus 的 `distance` 字段在 `metric_type=COSINE` 时返回的就是相似度（越大越相似）**，与 Qdrant 语义一致，可直接用 |
| 删除 | `scroll` 找 id → `delete(PointIdsList)` | `delete(filter='doc_id == "..."')` | ✅ **Milvus 更优**：Qdrant 适配器有「单次 scroll 上限 10000 条、超限残留孤儿向量」的已知限制（源码 C8），**Milvus 用表达式删除天然没有这个限制** |
| 建集合 | `create_collection` + `create_payload_index`（幂等靠 try/except） | `has_collection()` 判断 + `create_collection(schema, index_params)` | ✅ **Milvus 更优**：有原生 `has_collection()`，不需要 try/except 吞异常 |
| 健康检查 | `get_collections()` | `list_collections()` 或 `get_load_state()` | ⚠️ Milvus 需要 **collection 已 load** 才能 search，`create_collection` 传 `index_params` 会自动 load |

> 💡 **设计要点**：`VectorHit.payload` 契约保持为 `dict`，适配器内部把「JSON 字段 `payload`」原样取回即可。
> 这样 `service.py` 里 `ContractChunk.from_payload(hit.payload)`（已存在）**零改动就能工作**。

#### D1.5 备选方案 B（后期可选，本次不做）：用 Milvus 原生稀疏向量替掉自研 BM25

Milvus 2.4+ 支持 `SPARSE_FLOAT_VECTOR`，2.5+ 提供内置 BM25 全文检索函数（`Function` + `FunctionType.BM25`），
可在**同一个 collection 里做稠密+稀疏混合检索**，直接由 Milvus 做 RRF 融合。

**为什么本次不做**：
- 现有 `BM25Store`（纯 Python、pickle、零依赖）**已实现且有 12 个测试覆盖**，替换是负收益
- Milvus 的稀疏检索会**把两种检索引擎的能力绑死**，失去「BM25 可独立替换」的灵活性
- 本次重点是**先让 Milvus 稳定跑起来**，一次只换一个变量

**什么时候做**：当法规库导入到万级以上、且 `BM25Store` 的 O(N) 全量扫描（见分析报告 F14）成为瓶颈时。
届时只需新增一个 `MilvusSparseStore(SparseStorePort)` 适配器 —— **又是「加适配器」而不是改业务**。

#### D1.6 决策清单（请勾选）

- [ ] **D1-a**：确认采用「**Milvus 为生产向量库 + InMemory 为默认/单测 + Qdrant 保留为对照与回退**」
- [ ] **D1-b**：确认 Windows 下的运行方式：`Docker standalone` / `WSL2 Lite` / `仅演示环境用 Milvus`
- [ ] **D1-c**：确认 Milvus 版本（建议 **2.5.x 稳定版**，避开 3.0 尚在演进的分支）
- [ ] **D1-d**：确认是否需要把该决策**回写进 `RAG_MODULE_DESIGN.md` §1.3**（建议要，否则下次评审会再吵一次）

---

### D2 Agent 编排：LangGraph 版本、LLM 接入方式、状态持久化

#### D2.1 已装版本实查（这是决策的地基）

`[实测]` 你的 `.venv` 里**已经装了**：

```
langgraph 1.2.11          （含 graph / prebuilt / checkpoint / func / store / pregel / channels / types）
langgraph_prebuilt 1.1.0
langgraph_sdk 0.4.3
langchain_core 1.6.0      （含 agents / retrievers / cross_encoders / structured_query / stores）
langsmith 0.11.1
openai 3.3.1              （官方 SDK）
```

而 `packages/ai-engine/pyproject.toml` 声明的却是 `langgraph>=0.2` + `openai>=1.30` ——
**声明与实际严重脱节**（0.2 → 1.2.11 是跨大版本的）。这会导致两个后果：
1. 别人 `uv sync` 装到的可能是 0.2 系列，**API 完全不同**，代码直接跑不起来
2. 你本地能跑，别人不能跑 —— 最难排查的一类问题

**动作**：把 `ai-engine/pyproject.toml` 的版本约束**收紧到已实测的版本**（见 T-B0）。

#### D2.2 决策 1：LLM 接入方式 —— `langchain-openai` 还是裸 `openai` SDK？

`[实测]` **`langchain-openai` 未安装**（只有 `openai`)。

| 方案 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| **① 装 `langchain-openai`，用 `ChatOpenAI`** | 与 LangGraph 原生契合；`.with_structured_output()` 直接映射到 Pydantic 模型（本项目契约是 Pydantic，**天然契合**）；`bind_tools()` 一行接工具 | 多一层抽象；`openai 3.3.1` 与 `langchain-openai` 的版本兼容需核对 | ✅ **推荐**（本项目 90% 的输出都是结构化 Pydantic） |
| ② 裸 `openai` SDK + 自己写适配器 | 依赖最少、行为完全可控 | 要自己写 function-calling 循环、重试、流式、结构化解析 —— **重复造轮子** | 仅当①出兼容问题时的回退 |

> 💡 **关键契合点**：`ai_engine/agents/auditor.py` 里 `EnhancedRiskItem` / `ReasoningStep` / `PreciseCitation`
> **已经是严格的 Pydantic 模型**。用 `ChatOpenAI(...).with_structured_output(EnhancedRiskItem)`
> 可以**直接拿到类型安全的输出，无需手写解析与校验**。这是本项目为 Agent 层省下的最大一块工作量。

**建议**：`uv add --package ai-engine langchain-openai`，并在 `ai_engine/llm/` 下做一个薄封装
（`get_chat_model()`），把「模型名 / 温度 / base_url / api_key」全部走 `CG_LLM_*` 环境变量，
**禁止在业务代码里直接 new 模型**（与既有 `CG_RAG_*` 配置风格保持一致）。

#### D2.3 决策 2：用 `create_react_agent` 还是自绘 `StateGraph`？

| 方案 | 适用 | 本项目是否合适 |
|---|---|---|
| `langgraph.prebuilt.create_react_agent` | 通用「LLM + 工具」ReAct 循环 | ❌ **不合适**。赛题要求固定的「分类→抽取→风险→矛盾→比对→报告」流水线 + 每步 CoT + 每步溯源 + 可降级。ReAct 的自由循环**无法保证阶段顺序与产出完整性** |
| **自绘 `StateGraph`** | 需要确定性阶段、可控降级、可挂 checkpoint | ✅ **推荐**。且赛题本身就是「多 Agent 协作」，自绘图更能体现设计能力 |

**推荐 Graph 拓扑（audit_graph）**：

```
        ┌──────────┐
        │  START   │
        └────┬─────┘
             ▼
      ┌─────────────┐
      │  classify   │  合同类型分类（≥85% 指标）→ 写入 state.contract_type
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │  extract    │  要素抽取（F1 ≥80% 指标）→ list[ContractElement]（含 bbox/confidence）
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │  risk_node  │  逐条款风险识别 → list[EnhancedRiskItem]（含 reasoningSteps + citations）
      └──────┬──────┘        ↑ 内部调用 RAG Tool（retrieve_contract_knowledge）
             ▼
      ┌─────────────┐
      │ conflict_nd │  跨条款矛盾 → list[CrossClauseConflict]
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │ standard_cmp│  标准条款比对 + **缺失检测**（用 EMPTY_RECALL 作为「缺失」信号）
      └──────┬──────┘
             ▼
      ┌─────────────┐
      │ report_draft│  汇总 + 健康分 → ReportDraft
      └──────┬──────┘
             ▼
        ┌──────────┐
        │   END    │
        └──────────┘
```

> ⚠️ **一处必须做对的设计**：`risk_node` 处理的是「逐条款」循环。**不要在 Graph 里为每个条款建一个节点**
> （节点数会在运行时爆炸、checkpoint 巨大）。正确做法是**在 `risk_node` 内部用 `Send` API 做并行 fan-out**，
> 或者**在节点内串行/并发遍历条款**，Graph 只保留 6 个固定阶段。
> 本项目合同通常几十到几百条条款，建议先用「节点内遍历 + 并发限流」，简单可控，必要时再上 `Send`。

#### D2.4 决策 3：状态持久化（checkpointer）

`[实测]` 已装 `langgraph_checkpoint 4.2.0`（**进程内 `InMemorySaver` 可用**），
但**没有** `langgraph-checkpoint-postgres` / `langgraph-checkpoint-redis`。

| 阶段 | 方案 | 说明 |
|---|---|---|
| 开发/单测 | `InMemorySaver` | 零依赖，立即可用 |
| 生产/多轮改写 | 需额外装 `langgraph-checkpoint-postgres`（配 Postgres，**compose 里已有**）或 `-redis` | 让「多轮 AI 改写」的会话状态**跨 Celery 进程存活** |

> 🔴 **这是 rewrite 功能的关键**：`worker` 是 Celery 多进程，若用 `InMemorySaver`，
> 用户在第二次追问时可能落到另一个 worker 进程 → **上下文丢失、看似"AI 失忆"**。
> **必须用外部 checkpointer**（推荐 Postgres，因为你已经有它了）。

#### D2.5 决策清单

- [ ] **D2-a**：确认 LLM 接入走 `langchain-openai`（推荐）还是裸 SDK
- [ ] **D2-b**：确认 Graph 采用**自绘 `StateGraph` + 6 阶段**拓扑
- [ ] **D2-c**：确认 checkpointer 选型（开发 `InMemorySaver` / 生产 `PostgresSaver`）
- [ ] **D2-d**：确认 LLM 供应商与模型名（用于填 `CG_LLM_*`，并注意 **JSON 结构化输出的稳定性**，这直接决定风险识别的达标率）

---

### D3 OCR：选哪个开源项目，怎么适配

#### D3.1 选型判据（按本项目真实需求排序）

合同场景对 OCR 的要求**与通用 OCR 很不一样**，按重要性排：
1. **中文准确率**（尤其宋体小四、仿宋合同正文）
2. **逐字/逐行坐标**（必须回流到 BBox → 前端高亮，这是赛题硬性交付）
3. **表格与段落结构保留**（条款 AST 依赖它）
4. **印章检测与识别**（创新点，通用 OCR 项目**默认都没这个能力**）
5. **Windows + CPU 可跑、部署体积可控**（评委现场演示不能要求 GPU）

#### D3.2 候选对比

| 候选 | 中文 | 坐标 | 表格/版面 | 印章 | Windows/CPU | 评价 |
|---|---|---|---|---|---|---|
| **PaddleOCR 2.x（PP-OCRv4）** | 优 | ✅ | 需配 PP-StructureV2 | ❌ 无 | ✅（需 `paddlepaddle`） | 项目现在声明的就是这个；**稳定但偏旧** |
| **PaddleOCR 3.x（PP-OCRv5）** | **更优** | ✅ | ✅ PP-StructureV3 | ❌ 无（有版面印章**区域**，无识别） | ✅ | ⚠️ **3.x 与 2.x API 不兼容**（`PaddleOCR(...)` 参数与返回结构都变了），需按 3.x 重写适配器 |
| **RapidOCR**（ONNXRuntime 版 PaddleOCR） | 优 | ✅ | ❌ 需另配 | ❌ | ✅✅（**无需 paddlepaddle，纯 onnx**） | 部署最轻、安装最省心；**作为兜底/降级路线的极佳选择** |
| **MinerU**（原 Magic-PDF） | 优 | ✅ | ✅✅（版面+公式+表格最强） | ❌ | 🟡（依赖较重） | 版面解析最强，适合做「复杂扫描件」的高质量路线 |
| **Umi-OCR** | 优 | 🟡 | ❌ | ❌ | ✅（桌面工具） | 定位是桌面软件，**不适合作为库集成** |
| **Surya / Marker** | 一般（偏英文） | ✅ | ✅ | ❌ | 🟡（需 torch） | ⚠️ **需 torch，与项目红线冲突，直接排除** |

#### D3.3 推荐方案

> ✅ **主路线：PaddleOCR 3.x（PP-OCRv5 + PP-StructureV3）**
> ✅ **降级/兜底：RapidOCR**（当 `paddlepaddle` 装不上、或需要极小部署体积时自动切换）
> ✅ **印章：必须自己补**，不要指望通用 OCR（见 D3.4）

**理由**：
1. 你已经有 `paddleocr` 且周边依赖（opencv / pyclipper / shapely / scikit-image）**全都装好了**，只差推理引擎
2. PP-StructureV3 直接给版面区域（标题/段落/表格/图），**正好对上 `doc_processor/layout` 的 `BlockType` 枚举**
3. RapidOCR 作为兜底的价值极高：**它让 OCR 不再是「必须装 paddlepaddle 才能跑」的单点**

> ⚠️ **重要区分**：不管选哪个，**都必须走「数字版 PDF 优先直抽」的分流策略**（见 T-C2）。
> 数字版 PDF 用 `pdfplumber` 抽文本+逐词坐标，**比 OCR 更准更快，且坐标天然精确**。
> 只有扫描件/图片才走 OCR。**很多团队在这里犯的错误是对所有 PDF 都跑 OCR，白白引入误差。**

#### D3.4 印章检测：本项目最大的「无现成方案」点

**残酷现实**：`doc_processor/ocr/__init__.py` 里定义的 `SealDetection(extracted_text, confidence)`
要求「检测印章位置 + **识别印章内文字**（公司名/税号）」，而：
- PP-StructureV2/V3 只能给出「印章**区域**」，**不能识别印章文字**
- 通用 OCR 对**红色圆形印章**识别率极低（文字沿圆弧排列 + 红色背景 + 与签名重叠）

**推荐三段式方案**：

| 步骤 | 做法 | 难度 |
|---|---|---|
| ① 印章**区域检测** | **颜色分割**（HSV 红色掩膜）+ 形态学 + 轮廓筛选（面积/圆度）——传统 CV 即可，无需模型 | 低 |
| ② 印章**文字识别** | **极坐标展开**（把圆环拉直成水平文本行）→ 再送 OCR 引擎 | 中 |
| ③ 与公司名**比对** | 展开后的文字与合同甲乙方名称做模糊匹配 → 判定「印章与签约主体一致」 | 低 |

> 💡 **②的极坐标展开是印章识别的标准做法，且用 `opencv` 就能实现（已装）**。
> 这条路线**不依赖任何深度学习模型**，完全契合本项目「CPU 可跑、低配部署」的约束，
> 且实现难度远低于训练/寻找印章专用模型。**建议按此思路做，不要去找印章识别模型。**

#### D3.5 决策清单

- [ ] **D3-a**：确认主路线 PaddleOCR **3.x（PP-OCRv5）**，还是保守用 **2.x（PP-OCRv4）**
      （⚠️ 3.x API 不兼容 2.x，`packages/doc-processor/pyproject.toml` 现声明 `paddleocr>=2.7`，需同步改）
- [ ] **D3-b**：确认是否加入 **RapidOCR** 作为降级路线
- [ ] **D3-c**：确认印章方案采用「HSV 颜色分割 + 极坐标展开 + 模糊比对」三段式
- [ ] **D3-d**：确认 OCR 是否也用于**手写签名**识别（若是，难度显著上升，建议本次只做「存在性检测」不做识别）

---

### D4 语义服务：embedding 与 reranker 怎么落地

> 这是**赛题达标的真正瓶颈**（分析报告 F1）。没有它，RAG 召回率上不去，风险识别 ≥75% 无从谈起。

#### D4.1 现状（必须改）

`[实读]` + `[实测]`：
- `CG_RAG_EMBED_MODE` 默认 `hash` → **伪向量，无同义/近义能力**
- `CG_RAG_RERANK_MODE` 默认 `noop` → **无精排**
- `docker-compose.yml` **既没有 embedding 服务，也没有 rerank 服务** → 就算改成 `http` 也没对端
- 实测冒烟 3 条查询 1 条空召回，**就是这两个默认值的直接后果**

#### D4.2 部署形态对比

| 方案 | 说明 | 适合 |
|---|---|---|
| **TEI（HuggingFace Text Embeddings Inference）** | 官方镜像，专为 embedding/rerank 优化，**已适配 `/embed` 与 `/rerank` 端点**（`CG_RAG_EMBED_PATH` 注释里就写着 TEI） | ✅ **推荐**：项目作者显然已经为 TEI 预留了配置 |
| vLLM | 以 LLM 推理为主，embedding 支持较新 | 若你同时要自部署 LLM |
| Xinference | 一个服务同时提供 embedding + rerank + LLM | ✅ 想少起容器时的好选择 |
| Ollama | 简单，但 embedding 模型选择少、rerank 支持弱 | 本地快速试验 |
| **云端 API**（OpenAI / 智谱 / DashScope 等） | 零部署成本 | ✅ **若评分现场网络可用，这是最快达标的路** |

#### D4.3 推荐

```
Embedding : BAAI/bge-m3（1024 维）—— 项目已锁定 1024 维，不要改（改维度=重建索引）
Reranker  : BAAI/bge-reranker-v2-m3 —— 项目 CG_RAG_RERANK_MODEL 默认值就是它
形态      : 开发期用「云端 API 或 Ollama」快速验证；演示期用「TEI 容器」保证离线可跑
```

> ✅ **好消息**：`rag_core.adapters.embedding.HttpEmbedder` 与 `adapters.http_reranker.HttpReranker`
> **都已经写好并有测试**（`test_http_reranker.py` 存在）。你**只需要起服务 + 改 2 个环境变量**：
> ```
> CG_RAG_EMBED_MODE=http
> CG_RAG_EMBED_BASE_URL=http://embeddings:80      # TEI /embed
> CG_RAG_EMBED_PATH=/embed
> CG_RAG_RERANK_MODE=http
> CG_RAG_RERANK_HTTP_URL=http://reranker:80
> CG_RAG_RERANK_PATH=/rerank
> ```
> **这是全项目投入产出比最高的一处改动。**

#### D4.4 决策清单

- [ ] **D4-a**：确认 embedding/rerank 服务形态（TEI 容器 / 云端 API / Xinference）
- [ ] **D4-b**：确认是否有 GPU（有 → TEI GPU 镜像；无 → CPU 镜像，bge-m3 在 CPU 上单条约 50~200ms，可接受）
- [ ] **D4-c**：确认「换模型 = 重建索引」的操作流程（`chunk.embedding_model` 字段已在契约里，可据此识别旧索引）

---

## 2. WS-A · RAG 工作流（Milvus 为核心）

### T-A0 前置修复（**必须最先做**，约 0.5 人日，解除 4 个 P0/P1 阻塞）

> 这 4 项**不做**，后面的任务全部会踩坑。它们与 Milvus 无关，属于「清地基」。

| 编号 | 任务 | 落点 | 验收 |
|---|---|---|---|
| **T-A0-1** | 装 `celery`，让 `async-worker` 可导入 | `apps/async-worker/pyproject.toml` | `python -c "import worker.main"` 不报错 |
| **T-A0-2** | 给 `SparseHit` 加 `payload: dict[str, Any]`，并让 `_fuse()` 用 `ContractChunk.from_payload()` 还原**稀疏路独占候选** | `packages/rag-contracts/rag_contracts/ports.py` + `rag-core/rag_core/service.py` | 仅 BM25 命中的候选**不再被丢弃**；`retrieve.sparse` 日志中的 `sparse_only` 计入 `retrieved_docs` |
| **T-A0-3** | 补 `ai-engine` 的 workspace 依赖声明 | `packages/ai-engine/pyproject.toml` | `uv sync --all-packages` 后 `import ai_engine, rag_core` 均成功 |
| **T-A0-4** | 新增根 `pytest.ini`（固化 `pythonpath` / `testpaths`）+ 补齐 `known-first-party` 与 `mypy_path` | 根 `pytest.ini`、根 `pyproject.toml` | `pytest` 一条命令跑出 140+ passed，无需手工设 `PYTHONPATH` |

**T-A0-2 是本文档中性价比最高的一项**：约 40 行代码，直接恢复「精确编号检索」（第 X 条 / 法条号）
这一 BM25 在合同场景最不可替代的能力。

**T-A0-2 的施工要点**（避坑）：
- `SparseHit` **不在** 4 份导出的 JSON Schema 之列 → **改它不触发「生成物红线」**
- `BM25Store.upsert()` 目前只存 `_texts`，需**同时存 `payload`**（在 `upsert` 里 `chunk.to_payload()`）
- 反序列化要兼容旧 pickle（`_load()` 里 `payload` 缺失时给默认 `{}`），否则已有索引文件会加载失败

---

### T-A1 实现 `MilvusVectorStore`（**本次核心交付物**，约 1.5 人日）

**落点**：`packages/rag-core/rag_core/adapters/milvus_store.py`（新建）

> ⚠️ **命名约定**：必须叫 `MilvusVectorStore` 并在 `adapters/__init__.py` 导出，
> 与 `QdrantVectorStore` 保持对称（`RAG_MODULE_DESIGN.md` §1.3.1 的类图里预留的就是这个名字）。

**参考骨架**（请按此结构实现，细节按 pymilvus 实际版本核对）：

```python
"""Milvus 向量库适配器：实现 `rag_contracts.ports.VectorStorePort`。

设计要点（对齐 QdrantVectorStore 的既有约定）：
1. `pymilvus` **仅在本模块内延迟导入** —— 默认路径（InMemoryVectorStore）零影响。
2. `chunk_id` 直接作 Milvus 主键（VARCHAR），与 Qdrant 的 point_id 语义一致。
3. payload 双层存储：完整 chunk 存 JSON 字段 `payload`；**参与过滤的 6 个字段冗余成顶层标量列**
   （Milvus 无法高效过滤 JSON 嵌套键，必须冗余，见 D1.4）。
4. 所有异常统一包装为 `StoreUnavailableError`，与既有适配器一致（`retrieve()` 据此降级）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag_contracts.errors import StoreUnavailableError
from rag_contracts.ports import VectorHit, VectorRecord
from rag_contracts.query import RetrievalFilters

# 需要建标量索引 / 参与过滤的字段（与 qdrant_store.KEYWORD_INDEX_FIELDS 语义对齐）
_SCALAR_FIELDS: tuple[str, ...] = (
    "source_type", "chunk_type", "contract_type", "doc_id", "law_code", "category",
)
# VARCHAR 列宽：chunk_id 是 uuid5(36)，其余按业务上限留足余量
_MAX_LEN: dict[str, int] = {
    "chunk_id": 64, "doc_id": 200, "source_type": 32, "chunk_type": 32,
    "contract_type": 32, "law_code": 64, "category": 64,
}


class MilvusVectorStore:
    """Milvus 适配器（mode: server / lite）。

    Attributes:
        name: 适配器标识，写入日志 `store` 字段。
    """

    def __init__(
        self,
        *,
        mode: str = "server",
        uri: str = "http://localhost:19530",
        token: str = "",
        db_name: str = "default",
        collection: str = "contract_clauses",
        timeout_s: float = 5.0,
    ) -> None:
        self._mode = mode
        self._collection = collection
        self.name = f"milvus:{mode}"
        self._dim: int | None = None
        self._client: Any = self._build_client(mode, uri, token, db_name, timeout_s)

    @staticmethod
    def _build_client(mode: str, uri: str, token: str, db_name: str, timeout_s: float) -> Any:
        """构造 MilvusClient（延迟导入，缺包时给可执行的提示）。"""
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover
            raise StoreUnavailableError(
                "未安装 pymilvus，请改用 CG_RAG_VECTOR_STORE=inmemory 或安装该包"
            ) from exc
        if mode == "lite":
            # ⚠️ Milvus Lite 官方支持以 Linux/macOS 为主，Windows 上请用 server 模式（见 D1.2）
            return MilvusClient(uri=uri)
        kwargs: dict[str, Any] = {"uri": uri, "db_name": db_name}
        if token:
            kwargs["token"] = token
        return MilvusClient(**kwargs)

    # ---------------- 内部工具 ----------------
    @staticmethod
    def _escape(value: str) -> str:
        """转义 expr 字符串字面量（防注入 / 防语法错）。"""
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _build_expr(self, filters: RetrievalFilters | None) -> str:
        """把 RetrievalFilters 翻译为 Milvus `expr` 布尔表达式。

        与 Qdrant 版 `_build_filter` 语义等价，仅语法不同（见 D1.4）。
        """
        if filters is None:
            return ""
        clauses: list[str] = []
        if filters.source_types:
            vals = ", ".join(f'"{self._escape(s.value)}"' for s in filters.source_types)
            clauses.append(f"source_type in [{vals}]")
        if filters.chunk_types:
            vals = ", ".join(f'"{self._escape(c.value)}"' for c in filters.chunk_types)
            clauses.append(f"chunk_type in [{vals}]")
        if filters.contract_type:
            clauses.append(f'contract_type == "{self._escape(filters.contract_type.value)}"')
        if filters.law_codes:
            vals = ", ".join(f'"{self._escape(c)}"' for c in filters.law_codes)
            clauses.append(f"law_code in [{vals}]")
        if filters.doc_ids:
            vals = ", ".join(f'"{self._escape(d)}"' for d in filters.doc_ids)
            clauses.append(f"doc_id in [{vals}]")
        if filters.categories:
            vals = ", ".join(f'"{self._escape(c)}"' for c in filters.categories)
            clauses.append(f"category in [{vals}]")
        return " and ".join(clauses)

    @staticmethod
    def _row_of(record: VectorRecord) -> dict[str, Any]:
        """把 VectorRecord 摊平成 Milvus 一行（含冗余标量列）。"""
        chunk = record.chunk
        row: dict[str, Any] = {
            "chunk_id": chunk.chunk_id,
            "vector": list(record.vector),
            "payload": chunk.to_payload(),   # 完整切片，供 from_payload 还原
        }
        for field in _SCALAR_FIELDS:
            # None 在 Milvus 标量列中写作 ""，读取时再还原（避免 schema 声明 nullable 的兼容问题）
            value = getattr(chunk, field, None)
            row[field] = getattr(value, "value", value) if value is not None else ""
        return row

    # ---------------- VectorStorePort 实现 ----------------
    def ensure_collection(self, dim: int) -> None:
        """建集合（幂等）：主键 + 向量列 + JSON payload + 6 个标量列 + 索引。"""
        from pymilvus import DataType

        if dim != self._dim:
            self._dim = dim
        if self._client.has_collection(self._collection):
            return

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=_MAX_LEN["chunk_id"])
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=int(dim))
        schema.add_field("payload", DataType.JSON)
        for field in _SCALAR_FIELDS:
            schema.add_field(field, DataType.VARCHAR, max_length=_MAX_LEN.get(field, 200))

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="HNSW", metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        for field in _SCALAR_FIELDS:
            # INVERTED 标量索引：让 expr 过滤走索引而非全表扫
            index_params.add_index(field_name=field, index_type="INVERTED")
        # 传 index_params 会顺便完成 load，search 前无需再手动 load
        self._client.create_collection(self._collection, schema=schema, index_params=index_params)

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """幂等写入：主键为 chunk_id，重复写覆盖不产生副本。"""
        if not records:
            return 0
        rows = [self._row_of(r) for r in records]
        try:
            self._client.upsert(collection_name=self._collection, data=rows)
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus upsert 失败：{exc}") from exc
        return len(rows)

    def search(
        self, vector: Sequence[float], top_k: int, filters: RetrievalFilters | None = None
    ) -> list[VectorHit]:
        """稠密检索（COSINE）。"""
        try:
            results = self._client.search(
                collection_name=self._collection,
                data=[list(vector)],
                limit=max(1, int(top_k)),
                filter=self._build_expr(filters) or "",
                output_fields=["payload"],
                search_params={"metric_type": "COSINE", "params": {"ef": 64}},
            )
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus search 失败：{exc}") from exc

        hits: list[VectorHit] = []
        for row in (results[0] if results else []):
            # ⚠️ metric_type=COSINE 时 Milvus 的 distance 即相似度（越大越相似），与 Qdrant 语义一致
            entity = row.get("entity") or {}
            payload = entity.get("payload") or {}
            hits.append(
                VectorHit(chunk_id=str(row.get("id", "")), score=float(row.get("distance", 0.0)),
                          payload=dict(payload))
            )
        return hits

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除。

        ✅ 优于 Qdrant 版：用表达式删除，**没有**「单次 scroll 上限 10000 条」的限制
        （Qdrant 适配器的已知限制 C8 在此不复现）。
        """
        expr = f'doc_id == "{self._escape(doc_id)}"'
        try:
            result = self._client.delete(collection_name=self._collection, filter=expr)
        except Exception as exc:
            raise StoreUnavailableError(f"Milvus delete 失败：{exc}") from exc
        # 返回结构随版本而异，兼容取值；拿不到计数时退化为 0
        if isinstance(result, dict):
            return int(result.get("delete_count", 0) or 0)
        return int(getattr(result, "delete_count", 0) or 0)

    def health(self) -> bool:
        """健康检查。"""
        try:
            self._client.list_collections()
            return True
        except Exception:  # noqa: BLE001 - 健康检查失败即不可用，不抛
            return False

    def count(self) -> int:
        """当前集合内实体数（对齐 QdrantVectorStore.count）。"""
        try:
            stats = self._client.get_collection_stats(self._collection)
            return int(stats.get("row_count", 0))
        except Exception:  # noqa: BLE001 - 集合不存在时返回 0
            return 0


__all__ = ["MilvusVectorStore"]
```

**⚠️ 实现时必须实测确认的 6 个点**（pymilvus 版本间有差异，**不要照抄后就认为对**）：

1. `MilvusClient` 的 `token` / `db_name` 参数名与默认值
2. `client.delete()` 的**返回结构**（dict 还是对象；字段名是 `delete_count` 还是别的）
3. `client.search()` 返回的 `entity` 是否真能取到 JSON 字段（部分版本需显式 `output_fields`）
4. `metric_type=COSINE` 时 `distance` 的含义是否符合预期（**必须用已知向量对做一次正反向验证**）
5. `index_params.add_index(field_name=..., index_type="INVERTED")` 在目标版本是否支持
6. VARCHAR 主键 `max_length` 的边界（uuid5 是 36 字符，64 安全）

> 💡 **强烈建议**：写完后**先跑一个最小脚本**（建集合 → 写 3 条 → 查 1 条 → 删 1 条 → `count()`），
> 确认这 6 点全部 OK，再往上接 `RAGService`。**不要在没验证适配器的情况下直接跑端到端**——
> 那时报错会混在 `retrieve()` 的降级路径里，极难定位。

---

### T-A2 配置与工厂接线（约 0.5 人日）

| 文件 | 改动 | 要点 |
|---|---|---|
| `rag-core/rag_core/config.py` | 新增 7 个字段 | `milvus_uri` / `milvus_token` / `milvus_db_name` / `milvus_mode` / `milvus_timeout_s`，并**把 `vector_store` 的 `Literal` 扩为 `"inmemory" \| "qdrant" \| "milvus"`** |
| `rag-core/rag_core/factory.py` | `build_vector_store()` 加 1 个分支 | 复制 qdrant 分支的写法（**延迟导入** `MilvusVectorStore`） |
| `rag-core/rag_core/adapters/__init__.py` | 导出 `MilvusVectorStore` | 与 `QdrantVectorStore` 对称 |
| `.env.example` | 补 `CG_RAG_MILVUS_*` 段 | 说明 server / lite 两种模式与 Windows 注意事项 |
| `docker/docker-compose.yml` | 新增 `etcd` + `milvus`（MinIO 已有，可复用/另起） | 见 T-A3 |

**红线**：`vector_store` 的 `Literal` 扩值时**不要删掉旧值**（`inmemory` / `qdrant` 必须保留，
否则默认路径与回退路径同时失效）。

---

### T-A3 Milvus 容器编排（约 0.5 人日）

**落点**：`docker/docker-compose.yml`

需要新增 2 个服务（MinIO 可复用现有实例，也可为 Milvus 单独起一个）：

```yaml
  etcd:
    image: quay.io/coreos/etcd:v3.5.16
    environment:
      - ETCD_AUTO_COMPACTION_MODE=revision
      - ETCD_AUTO_COMPACTION_RETENTION=1000
      - ETCD_QUOTA_BACKEND_BYTES=4294967296
    command: etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    volumes: ["etcddata:/etcd"]

  milvus:
    image: milvusdb/milvus:v2.5.x          # 用具体小版本，勿用 latest
    command: ["milvus", "run", "standalone"]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000            # 复用 compose 现有的 minio
      MINIO_ACCESS_KEY_ID: minioadmin
      MINIO_SECRET_ACCESS_KEY: minioadmin
    ports: ["19530:19530", "9091:9091"]    # 19530=gRPC 业务端口，9091=健康检查
    depends_on: [etcd, minio]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9091/healthz"]
      interval: 10s
      timeout: 5s
      retries: 10
```

**验收**：`curl http://localhost:9091/healthz` 返回健康；`pymilvus` 能 `list_collections()`。

**注意**：Milvus 启动比 Qdrant 慢（30~60 秒），`api` / `worker` 服务**不要用 `depends_on: service_started`**，
否则启动竞态会导致首次检索降级。建议加健康检查条件或在应用层做重试（`RAGService` 已有降级，但会静默降级一次）。

---

### T-A4 Milvus 集成测试（约 0.5 人日）

**落点**：`packages/rag-core/tests/test_milvus_store.py`（新建）

**关键设计**：默认路径（联网/无 Milvus）下**必须自动跳过**，不能污染现有的「140 passed」离线基线：

```python
import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("CG_RAG_TEST_MILVUS_URI"),
    reason="未设置 CG_RAG_TEST_MILVUS_URI，跳过 Milvus 集成测试（保持离线基线）",
)
```

**必须覆盖的断言**（每条都对应 D1.4 里的一个坑）：

| 断言 | 验证的是什么 |
|---|---|
| `ensure_collection` 连调 2 次不抛异常 | 幂等性（对应 `has_collection` 分支） |
| `upsert` 同一 `chunk_id` 两次 → `count()` 仍为 1 | 幂等写入（不产生副本向量） |
| `search` 返回的 `payload` 能 `ContractChunk.from_payload()` 还原且与写入等值 | **payload 往返一致性**（最容易出错的一环） |
| 按 `source_type` / `law_code` 过滤能正确命中 | **expr 翻译正确性**（D1.4 的核心风险） |
| 过滤条件含 `"` 的值不报错 | `_escape` 生效 |
| `delete_by_doc` 后 `count()` 减少且 `search` 不再返回 | 删除语义 |
| **同一 query 在 Milvus 与 InMemory 上的 Top-10 chunk_id 重合度 ≥ 0.8** | **跨实现一致性**（迁移正确性的最终判据） |

---

### T-A5 ~ T-A10 后续任务速览

| 编号 | 任务 | 落点 | 依赖 | 说明 |
|---|---|---|---|---|
| **T-A5** | 接入 embedding HTTP 服务 | 仅改环境变量 + compose 加 TEI 容器 | D4 | **投入产出比最高**；代码侧零改动 |
| **T-A6** | 接入 HTTP reranker | 仅改环境变量 + compose 加 rerank 容器 | D4 | 同上；注意 `CG_RAG_RERANK_THRESHOLD` 需实测调参 |
| **T-A7** | 让 8 个「声明了但没人消费」的配置项真正生效 | `config.py` / `factory.py` / `service.py` | — | 见分析报告 F8；重点是 `timeout_ms` / `max_retries` / `max_query_chars`（Agent 三道防线依赖它们） |
| **T-A8** | 建评测集 + 评测脚本（Recall@5 / MRR） | `scripts/eval_recall.py`（新建）+ `scripts/data/gold.jsonl` | — | ⚠️ **赛题达标的度量工具，越早越好**。可先用「标准条款原文作 query、其自身为 gold」自构造弱标注集 |
| **T-A9** | 层级切片 `SectionAwareChunker` | `rag_core/chunking/` | — | 替换 `SimpleChunker` 但**输出契约不变**（红线）。当前 `SimpleChunker` 已可用，非阻塞 |
| **T-A10** | Qdrant → Milvus 数据迁移脚本（仅当已有真实语料） | `scripts/migrate_qdrant_to_milvus.py` | T-A1 | 若索引是新建的，**此任务可跳过** |

---

## 3. WS-B · Agent 工作流（LangGraph + LangChain）

### T-B0 依赖与版本收口（0.3 人日，**必须最先做**）

| 动作 | 落点 | 验收 |
|---|---|---|
| 收紧 `langgraph` 版本约束到实测值（`>=1.2,<2.0`），补 `langchain-core` | `packages/ai-engine/pyproject.toml` | 与 `.venv` 实际版本一致 |
| 装 `langchain-openai`（若 D2-a 选①） | 同上 | `from langchain_openai import ChatOpenAI` 成功 |
| 装 checkpointer（若 D2-c 选 Postgres） | 同上 | `from langgraph.checkpoint.postgres import PostgresSaver` 成功 |
| 补 workspace 依赖（`rag-contracts` / `rag-core` / `cg-observability`） | 同 T-A0-3 | `import ai_engine, rag_core` 成功 |

> ⚠️ **不要跳过这一步**。当前 `langgraph>=0.2` 的声明在别人机器上会装到 0.2.x，
> 而 0.2 → 1.2 之间 `StateGraph` 的 API、`add_messages`、checkpointer 接口**都有变化**，
> 会产生「我这儿能跑他那儿不能跑」的幽灵问题。

---

### T-B1 LLM 适配层（0.5 人日）

**落点**：`packages/ai-engine/ai_engine/llm/`（**新建目录**）

```
ai_engine/llm/
├── __init__.py      导出 get_chat_model / LlmSettings
├── config.py        LlmSettings（env_prefix="CG_LLM_"）
└── client.py        get_chat_model() —— 全局唯一模型构造入口
```

**设计约束**（与既有 `CG_RAG_*` 风格严格一致）：

```python
class LlmSettings(BaseSettings):
    """LLM 配置。env_prefix=CG_LLM_，默认值必须可离线构造（不在实例化时联网）。"""
    model_config = SettingsConfigDict(env_prefix="CG_LLM_", env_file=".env", extra="ignore")

    model: str = "gpt-4o-mini"        # 或 qwen-plus / glm-4 等
    base_url: str = ""                # 兼容 OpenAI 协议的任意网关
    api_key: str = "none"
    temperature: float = 0.1          # 合同审核场景要低温度，保证可复现
    timeout_s: float = 60.0
    max_retries: int = 2
```

**三条红线**：
1. **禁止**在业务节点里直接 `ChatOpenAI(...)` —— 一律走 `get_chat_model()`（便于统一换模型/打点/限流）
2. **禁止**把 `api_key` 写进代码 —— 只走环境变量
3. **结构化输出必须用 `with_structured_output(<Pydantic模型>)`** —— 不要自己解析 JSON 字符串

> 💡 `ai_engine/agents/auditor.py` 的 `EnhancedRiskItem` / `RewritingResponse` 等**已经是严格的 Pydantic 模型**，
> 直接复用即可。**这是本项目为 Agent 层省下的最大工作量。**

---

### T-B2 Graph 状态定义（0.5 人日）

**落点**：`packages/ai-engine/ai_engine/graphs/state.py`（新建 `graphs/` 包）

**这是整个 Agent 层最需要想清楚的一个文件**：State 一旦定了，节点签名就定了。

```python
"""audit_graph 的全局状态定义。

设计要点：
1. **只放"跨节点需要传递"的东西**，节点内部临时变量不要进 State（否则 checkpoint 巨大）。
2. 所有集合字段用 `operator.add` 做 reducer —— 支持未来用 `Send` 做并行 fan-out 时不互相覆盖。
3. State 里的领域对象**全部复用 ai_engine.agents 的既有 Pydantic 模型**，不新造类型。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from ai_engine.agents.auditor import EnhancedRiskItem
from ai_engine.agents.conflict import CrossClauseConflict


class AuditState(TypedDict, total=False):
    """审核图的共享状态。`total=False` 允许节点只返回自己改写的键（LangGraph 会做部分更新）。"""

    # ---- 输入（由 worker 注入，节点只读） ----
    contract_id: str
    file_key: str                      # MinIO 对象键
    clauses: list[dict[str, Any]]      # 来自 doc_processor 的条款 AST（text + bbox + clause_id）
    raw_text: str
    tolerance: str                     # STRICT | BALANCED | LOOSE

    # ---- 阶段产物（节点写） ----
    contract_type: str                 # classify 产出，喂给 RAG 的 contract_type 过滤
    elements: list[dict[str, Any]]     # extract 产出（ContractElement）
    risks: Annotated[list[EnhancedRiskItem], operator.add]        # risk_node 产出
    conflicts: Annotated[list[CrossClauseConflict], operator.add] # conflict_node 产出
    missing_clauses: Annotated[list[dict[str, Any]], operator.add] # standard_compare 产出的"缺失项"
    health_score: float                # 健康分
    report_draft: dict[str, Any]       # report_draft 产出

    # ---- 可观测与降级（贯穿全图，必须保留） ----
    trace_id: str
    warnings: Annotated[list[str], operator.add]
    degraded: bool
    stage_latency_ms: dict[str, int]
```

> ⚠️ **必须注意**：State 里放 Pydantic 对象时，checkpointer 序列化需要它能
> `model_dump()` / `model_validate()`。`EnhancedRiskItem` 满足；若将来放自定义类，请确保可序列化。

---

### T-B3 audit_graph 拓扑与节点实现（3 人日，**本工作流主战场**）

**落点**：`packages/ai-engine/ai_engine/graphs/audit_graph.py`（新建）

```python
"""风险审核主图（对应 AI核心开发文档 §3.2 的 6 阶段流水线）。

拓扑：START → classify → extract → risk_node → conflict_node → standard_compare → report_draft → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .state import AuditState


def build_audit_graph(*, checkpointer=None, llm=None, retriever=None):
    """构造审核图。

    Args:
        checkpointer: 状态持久化器；None 则用进程内 saver（多进程 worker 下会丢上下文，见 D2.4）。
        llm: LLM 客户端（便于单测注入假模型）。
        retriever: RAG 检索器（便于单测注入假检索）。

    Returns:
        已 compile 的图，可直接 `invoke(state, config={"configurable": {"thread_id": contract_id}})`。
    """
    graph = StateGraph(AuditState)

    graph.add_node("classify", make_classify_node(llm))
    graph.add_node("extract", make_extract_node(llm))
    graph.add_node("risk_node", make_risk_node(llm, retriever))
    graph.add_node("conflict_node", make_conflict_node(llm))
    graph.add_node("standard_compare", make_standard_compare_node(retriever))
    graph.add_node("report_draft", make_report_draft_node())

    graph.add_edge(START, "classify")
    graph.add_edge("classify", "extract")
    graph.add_edge("extract", "risk_node")
    graph.add_edge("risk_node", "conflict_node")
    graph.add_edge("conflict_node", "standard_compare")
    graph.add_edge("standard_compare", "report_draft")
    graph.add_edge("report_draft", END)

    return graph.compile(checkpointer=checkpointer)
```

**六个节点的实现要点**（每个都要**独立可测**——用假 llm / 假 retriever 注入）：

| 节点 | 输入 | 输出 | 关键要求 |
|---|---|---|---|
| `classify` | `raw_text` / `clauses` | `contract_type` | 对应赛题「分类准确率 ≥85%」；用 `with_structured_output` 输出枚举；**few-shot 用标准合同类型描述** |
| `extract` | `clauses` | `elements` | 对应「要素 F1 ≥80%」；**必须回填 `bbox` 与 `confidence`**（缺失则前端无法高亮） |
| `risk_node` | `clauses` + `contract_type` | `risks` | **⚠️ 逐条款遍历，每个风险项必须带 `reasoningSteps` + `citations`**；每条都调 RAG Tool |
| `conflict_node` | `clauses` | `conflicts` | **不要做 O(n²) 全对比**：先用规则/关键词（付款、责任、期限）**筛出候选条款对**，再让 LLM 判定 |
| `standard_compare` | `risks` + RAG | `missing_clauses` | **关键**：用 RAG 的 `EMPTY_RECALL` / 低分命中作为「**缺失标准对照**」信号 —— 这是本项目「缺失检测」的实现机制 |
| `report_draft` | 全部 | `report_draft` + `health_score` | 纯汇聚，**不调 LLM**（保证可复现、可快速重跑） |

> 🔴 **`conflict_node` 的性能红线**：合同有 N 条条款时全对比较是 N² 次 LLM 调用。
> 100 条条款 = 4950 次调用 → **不可接受**。必须「**先规则粗筛、后 LLM 精判**」，
> 把候选对压到 20~50 对以内。这是本工作流最容易踩的性能坑。

---

### T-B4 RAG Tool 包装 + 三道防线（1.5 人日）

**落点**：`packages/ai-engine/ai_engine/agents/tools.py`（新建）

**好消息**：`rag_contracts/agent_tool.py` 里 **`build_function_calling_spec()` 已经写好了**，
并且**已正确处理 OpenAI strict 模式的 `$ref` 内联与 `required` 补全**。
你要做的是把它包成 LangChain 可用的工具，并把**三道防线真正实现**（目前只存在于文档里，见分析报告 F8）。

```python
"""Agent 可调用的 RAG 检索工具（对应 RAG_MODULE_DESIGN.md §2.3 的三道防线）。"""

from __future__ import annotations

from langchain_core.tools import tool

from rag_contracts.agent_tool import (
    EMPTY_CONTEXT_HINT, TOOL_NAME, AgentRAGToolInput, AgentRAGToolResult,
)


@tool(TOOL_NAME, args_schema=AgentRAGToolInput)
def retrieve_contract_knowledge(
    query: str, contract_type: str | None = None,
    source_types: list[str] | None = None, top_k: int = 5, reason: str | None = None,
) -> str:
    """从企业标准条款库、法规库与裁判文书库中检索可对照的条款或法条原文。

    当需要外部法律依据支撑风险判断、条款比对或缺失检测时调用。
    返回已精排的条款原文与来源标识；若无匹配会明确返回【缺失标准对照】。
    """
    ...
```

**三道防线必须真正落地**（当前是空的，见 F8）：

| 防线 | 现状 | 要做的事 |
|---|---|---|
| **① 超长拦截** | `CG_RAG_MAX_QUERY_CHARS` **无代码消费** | 在工具入口按「头 60% + 尾 40%」截断，并追加 warning（**保留尾部是因为合同争议语句常在段末**） |
| **② 空召回降级** | `RAGService` **已实现**（返回 `EMPTY_RECALL` + `EMPTY_CONTEXT_HINT`） | 工具层把 `degraded/EMPTY` 翻译成 `AgentRAGToolResult.status="EMPTY"`，**并强制 LLM 标注【缺失标准对照】** |
| **③ 超时重试** | `CG_RAG_TIMEOUT_MS` / `CG_RAG_MAX_RETRIES` **无代码消费** | 用 `tenacity`（或手写指数退避）包裹 `RAGService.retrieve()`；**仅对 `RagError.retryable=True` 重试**（错误码体系已就绪） |

> 💡 **工具返回值必须是字符串**（LLM 只能读文本）。
> `AgentRAGToolResult.to_tool_message()` **已经实现好了**，直接 `return result.to_tool_message()` 即可。
> ⚠️ 但**融合分/RRF 分未归一**（源码注释已说明是 (0,1) 小数、非归一），
> 若要写进 `relevanceScore` 必须过 `clamp_score()` —— **`rag_contracts.citation` 已提供**。

---

### T-B5 rewrite_graph + 分层 Memory（1.5 人日）

**落点**：`packages/ai-engine/ai_engine/graphs/rewrite_graph.py`（新建）

**拓扑**（比 audit 简单，但**必须挂 checkpointer**）：

```
START → load_context → rewrite → diff_emit → END
                          ↑
                     (多轮：用户追问时回到 rewrite)
```

**必须先解决的 3 件事**：

1. 🔴 **`ScopedClauseMemory` 有两份定义**（`agents/rewriter.py` 的 `list[dict]` 与 `memory/scoped_memory.py` 的 `list[DialogTurn]`）
   → **统一到 `memory/` 那份**，`agents/rewriter.py` 改为 import
2. 🔴 **`GlobalStateManager` 是进程内 dict**（分析报告 §3.5）
   → Celery 多进程下**必然丢状态**。至少要迁到 Redis（`CG_REDIS_URL` 已在配置里）
3. 🔴 **checkpointer 必须外部化**（见 D2.4），否则多轮改写会「AI 失忆」

**多轮改写的正确姿势**：
```python
config = {"configurable": {"thread_id": f"{contract_id}:{clause_id}"}}
graph.invoke(inputs, config)   # 同一 thread_id 即可续接上下文
```
> ✅ **thread_id 用 `contract_id:clause_id` 组合**——这正好对应 `ScopedClauseMemory` 的
> 「条款级作用域」设计，天然实现分层 Memory 的「局部上下文」那一层。

---

### T-B6 ~ T-B8 后续任务速览

| 编号 | 任务 | 落点 | 说明 |
|---|---|---|---|
| **T-B6** | Memory 持久化（Redis / Postgres） | `ai_engine/memory/` | 让 `GlobalStateManager` 可换后端；接口已定（`save`/`load`），只加实现 |
| **T-B7** | Prompt 模板库落地 | `packages/ai-engine/ai_engine/prompts/templates/*.prompt` | `PromptLibrary` **已实现**，但没有一个模板文件（`[实读]`）。至少补：classify / risk / rewrite / conflict 4 个 |
| **T-B8** | worker 接线 | `apps/async-worker/worker/tasks/run_audit.py` | 把 `NotImplementedError` 换成 `graph.invoke()`；推送 SSE 进度（`taskId/percent/stage`）；**注意 Celery 任务里不要传大对象**，只传 `contract_id` |

> ⚠️ **T-B8 的一个易错点**：`run_audit.py` 现在 import 了
> `from ai_engine.rag import HybridRetriever, Reranker` —— 这两个是**空壳**（分析报告 F6）。
> **接线时应改为使用 `rag_core` 的 `RAGService`**（经 `build_service_from_settings()`），
> 并把 `ai_engine/rag/hybrid_retriever` 改造为「薄桥接」或直接删除。
> **不要在两个地方各实现一遍检索。**

---

## 4. WS-C · OCR 工作流

### T-C0 依赖与引擎安装（0.3 人日）

| 动作 | 落点 | 说明 |
|---|---|---|
| 装推理引擎 | `uv add --package doc-processor paddlepaddle` | ⚠️ **当前只装了 `paddleocr`（包装层），没有引擎**，OCR 一行都跑不了（分析报告 F4） |
| 修正依赖声明 | `packages/doc-processor/pyproject.toml` | 现在声明 `paddleocr>=2.7` 但**没有 `paddlepaddle`** —— 声明不完整 |
| （可选）降级引擎 | `uv add --package doc-processor rapidocr-onnxruntime` | 作为「paddlepaddle 装不上」时的兜底（D3-b） |
| 删掉误加的重依赖 | 检查 `doc-processor` 是否引入 `torch` | **本项目红线禁止 torch**（`rag-core/pyproject.toml` 有明确注释） |

> ⚠️ **Windows 上 `paddlepaddle` 安装是常见坑**：若 `paddlepaddle` 装不上，
> **不要死磕** —— 直接切 RapidOCR（纯 onnxruntime，安装极轻），
> 因为 `OcrEngine` 的**输出契约已冻结**，换引擎不影响下游任何代码。

---

### T-C1 文档分流器（0.5 人日）

**落点**：`packages/doc-processor/doc_processor/router.py`（新建）

> 🔴 **这是整个 OCR 工作中最重要、也最容易被忽略的一步。**
> **不要对所有 PDF 都跑 OCR**——数字版 PDF 直接抽文本+坐标，**更准、更快、坐标天然精确**。
> 很多团队在这里引入不必要的识别误差，还拖慢解析速度。

```python
"""文档分流器：按文件类型与内容特征选择最优解析路径。

分流策略（priority 从上到下）：
1. DOCX            → python-docx（结构化程度最高，无需 OCR）
2. 数字版 PDF      → pdfplumber（逐词坐标，精度最高）
3. 扫描版 PDF      → pypdfium2 渲染为图片 → OCR
4. 图片            → 直接 OCR
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class DocKind(str, enum.Enum):
    DOCX = "DOCX"
    PDF_NATIVE = "PDF_NATIVE"
    PDF_SCANNED = "PDF_SCANNED"
    IMAGE = "IMAGE"


@dataclass
class RouteDecision:
    kind: DocKind
    page_count: int
    reason: str          # 写进日志/解析报告，便于排查"为什么这份走了 OCR"


def detect_native_pdf(path: str, *, min_chars_per_page: int = 50) -> bool:
    """判定 PDF 是否为数字版：抽样前若干页，统计可抽取字符数。

    判据：抽样页的平均可抽字符数 >= min_chars_per_page 即认为是数字版。
    阈值可调；扫描件通常为 0（无文本层）。
    """
    ...
```

> 💡 **PDF → 图片请用 `pypdfium2`（已安装）**，不要引入 `pdf2image`。
> `pdf2image` 依赖外部 poppler 二进制，**在 Windows 上部署极麻烦**；
> `pypdfium2` 是纯 pip 包，自带二进制，**零外部依赖**。

---

### T-C2 实现 `OcrEngine`（1 人日）

**落点**：`packages/doc-processor/doc_processor/ocr/engine.py`（新建；`ocr/__init__.py` 保持只导出契约）

**必须遵守的契约**（`[实读]` `ocr/__init__.py` **已冻结**，不要改）：

```python
@dataclass
class OcrResult:
    page: int; x: float; y: float; width: float; height: float
    text: str; confidence: float

@dataclass
class SealDetection:
    page: int; x: float; y: float; width: float; height: float
    extracted_text: str | None; confidence: float
```

**适配器骨架**（关键是把引擎原始输出翻译成上面两个契约）：

```python
"""OCR 引擎适配器：把开源 OCR 项目的输出归一化到 doc_processor 契约。

设计要点：
1. 引擎类**延迟导入**（与 rag_core 的适配器风格一致）—— 没装 paddleocr 也能 import 本模块。
2. 所有坐标一律换算到 PDF_POINTS（72dpi，左上原点）—— 复用 BBoxAligner，不要自己写缩放。
3. 引擎选择可配置（CG_OCR_ENGINE=paddle|rapid），便于装不上 paddlepaddle 时切换。
"""

from __future__ import annotations

from . import OcrResult, SealDetection


class PaddleOcrEngine:
    """基于 PaddleOCR 的实现。"""

    def __init__(self, *, lang: str = "ch", use_gpu: bool = False, dpi: int = 200) -> None:
        self._lang = lang
        self._use_gpu = use_gpu
        self._dpi = dpi              # 渲染 DPI：坐标换算用得上，必须记住
        self._engine = None          # 延迟到首次调用才构造（模型加载很慢）

    def _ensure_engine(self):
        """懒加载引擎（首次调用约 2~10 秒，勿放在模块顶层）。"""
        if self._engine is None:
            from paddleocr import PaddleOCR   # 延迟导入
            self._engine = PaddleOCR(lang=self._lang, use_gpu=self._use_gpu)
        return self._engine

    def ocr_page(self, image_path: str) -> list[OcrResult]:
        """对单页图片做 OCR。

        ⚠️ 不同 PaddleOCR 大版本的返回结构不同（2.x 是嵌套 list，3.x 是 dict/对象），
        本方法必须做**结构兼容层**，不要把某一种结构写死。
        """
        ...

    def detect_seals(self, image_path: str) -> list[SealDetection]:
        """印章检测（见 T-C6 的三段式方案；通用 OCR 不提供此能力）。"""
        ...
```

**⚠️ 版本兼容提示**：PaddleOCR 2.x → 3.x 的 `PaddleOCR()` 构造参数与 `ocr()` 返回结构**都不兼容**。
若 D3-a 选 3.x，请在适配器里做结构兼容，**并把版本钉死在 `pyproject.toml`**（勿用 `>=` 敞口）。

---

### T-C3 `LayoutAnalyzer` 实现（1 人日）

**落点**：`packages/doc-processor/doc_processor/layout/analyzer.py`

**契约已冻结**（`[实读]`）：`BlockType` 枚举（`TITLE` / `PARAGRAPH` / `TABLE` / `SEAL` / `SIGNATURE` / `UNKNOWN`）
+ `LayoutBlock(block_type, page, x, y, width, height, text)`。

**两个实现路线**：

| 路线 | 做法 | 建议 |
|---|---|---|
| **A. 数字版 PDF** | `pdfplumber` 的 `extract_words(extra_attrs=["size","fontname"])` + **字号/字体聚类**判标题层次 | ✅ 优先；合同标题通常靠字号区分，**规则法足够** |
| **B. 扫描件** | PP-StructureV2/V3 版面模型 | 仅在 A 不可用时用 |

> 💡 **路线 A 的性价比远高于 B**：合同文档的标题识别，用「字号 + 加粗 + 独立成行 + 编号正则」四条规则
> 就能做到 90%+ 准确率，**比调版面模型快得多**，且完全可解释。
> 建议：**先做 A，B 只在扫描件场景兜底**。

---

### T-C4 `NodeExtractor` 实现（1.5 人日）

**落点**：`packages/doc-processor/doc_processor/parser/extractor.py`

**契约已冻结**（`[实读]`）：`ParsedElement(key,label,value,confidence,is_custom)` + `ParsedClause(id,parent_id,label,text,level,children_ids)`。

**两个子任务**：

1. **要素抽取**（`extract_elements`）：合同甲乙双方、签订日期、金额、期限、管辖地
   —— 这是赛题「要素 F1 ≥80%」的对象。建议**规则正则为主 + LLM 补漏**：
   金额（`¥` / `元` / 大写）、日期（`YYYY年MM月DD日`）、期限（`N日/N个月`）都有强模式，规则命中率很高。
2. **条款 AST**（`extract_clauses`）：
   ⚠️ **直接复用 `rag_core.chunking.simple_chunker` 的编号正则**（`第X条` / `6.2` / `1)`），
   **不要重写一套**。区别只是：`SimpleChunker` 输出 `ContractChunk`（供检索），
   `NodeExtractor` 输出 `ParsedClause`（供 AST 拓扑）—— **两者应共享同一个编号识别正则**。

> 💡 **建议把编号正则提取到一个共享位置**（如 `doc_processor/parser/patterns.py`），
> `simple_chunker` 与 `extractor` 都 import 它。
> 否则「解析层认为的第 3 条」与「检索层认为的第 3 条」可能不一致 —— 这类 bug 极难排查。

---

### T-C5 `BBoxAligner` 补全（1 人日）

**落点**：`packages/doc-processor/doc_processor/aligner/aligner.py`

**现状**：`[实读]` `to_pdf_points()` **已有正确的 72dpi 缩放实现**，但缺两件事：

1. **OCR 像素坐标 → PDF_POINTS**：本质是 `72 / render_dpi` 缩放（渲染 DPI 必须从 T-C2 传进来）
2. 🔴 **`(char_offset → BBox)` 索引表**（源码 docstring 已列为 TODO）——**这是前端高亮的关键**：
   前端拿到的是「第 N 个字符到第 M 个字符」，需要**用这张表换算出 BBox 列表**才能画高亮框。

```python
class BBoxAligner:
    """坐标归一化 + 文本偏移量 ↔ BBox 映射索引。"""

    def to_pdf_points(self, page, x, y, w, h, src_dpi=72) -> NormalizedBBox:
        ...  # ✅ 已实现，保留

    def index_text_offsets(self, blocks: list, page_text: str) -> None:
        """建立 (char_offset, length) → BBox 的索引表。

        做法：按块/词顺序累加字符长度，记录每个词的 [start, end) 与其 BBox 的映射。
        前端高亮时用 (start, end) 去查表，取覆盖范围内的所有 BBox 外包框。
        """
        ...

    def locate(self, start: int, end: int) -> list[NormalizedBBox]:
        """把字符区间映射回 BBox 列表（可能有多个，跨行会返回多个框）。"""
        ...
```

> 🔴 **没有这张索引表，赛题的「报告含原文位置 + 前端高亮」就无法真正闭环**。
> 契约层已经有了 `ContractChunk.bbox`，但**它是「整条条款的框」，不是「命中句子的框」**。
> 若要高亮到句子级，必须补这一步。

---

### T-C6 印章检测（1.5 人日，**创新点，无现成方案**）

**落点**：`packages/doc-processor/doc_processor/ocr/seal.py`（新建）

**必须自己实现**（见 D3.4 的判断）：通用 OCR 项目**都不提供印章文字识别**。
按「**颜色分割 → 极坐标展开 → OCR → 模糊比对**」三段式实现，**全程不需要深度学习模型**：

```python
"""印章检测与识别（三段式，纯 OpenCV，无需深度学习模型）。

为什么不用模型：
1. 通用 OCR 对红色圆形印章识别率极低（文字沿圆弧排列 + 红底 + 与签名重叠）；
2. PP-Structure 只给"印章区域"，不识别文字；
3. 颜色分割 + 极坐标展开是本问题的经典解法，**可解释、可调参、CPU 毫秒级**。
"""

import cv2
import numpy as np

from . import SealDetection

_RED_LOW = np.array([0, 43, 46])       # HSV 红色下界（色相环绕，需两段）
_RED_HIGH = np.array([10, 255, 255])
_RED_LOW2 = np.array([156, 43, 46])    # 红色在 HSV 上跨 0/180 两端
_RED_HIGH2 = np.array([180, 255, 255])


def extract_red_mask(bgr: np.ndarray) -> np.ndarray:
    """提取红色掩膜（印章 / 手写签名常为红色）。"""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, _RED_LOW, _RED_HIGH)
    m2 = cv2.inRange(hsv, _RED_LOW2, _RED_HIGH2)
    mask = cv2.bitwise_or(m1, m2)
    # 形态学闭运算：把断开的笔画连起来（印章常被签名/文字遮挡）
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


def find_seal_regions(mask: np.ndarray, *, min_area: int = 3000) -> list[tuple[int, int, int, int]]:
    """从掩膜中筛出印章候选区域（按面积/圆度过滤，排除红色文字与表格线）。"""
    ...


def unwrap_ring(bgr: np.ndarray, cx: int, cy: int, r_in: int, r_out: int) -> np.ndarray:
    """极坐标展开：把圆环区域拉直成水平文本条。

    这是印章文字识别的关键一步 —— 展开后印章文字变成水平排布，普通 OCR 即可识别。
    """
    ...


def detect_and_read_seals(image_path: str, *, dpi: int = 200) -> list[SealDetection]:
    """完整流程：颜色分割 → 候选筛选 → 极坐标展开 → OCR → 组装 SealDetection。"""
    ...


def match_party_name(seal_text: str, party_names: list[str], *, threshold: float = 0.6) -> float:
    """印章文字与合同甲乙方名称的模糊匹配度 → 判定"印章与签约主体是否一致"。

    用 difflib.SequenceMatcher 即可（零依赖），无需引入 fuzzywuzzy。
    """
    ...
```

> 💡 **为什么这个方案能成立**：极坐标展开后，印章的环形文字变成一行水平文字，
> 这时**通用 OCR 的识别率会大幅提升**（它本来就是为水平文本设计的）。
> 这是印章识别的标准技巧，且 `opencv`（已装）就能全部实现。

---

### T-C7 `parse_doc` 任务接线（1 人日）

**落点**：`apps/async-worker/worker/tasks/parse_doc.py`

**完整链路**（把前面所有任务串起来）：

```
MinIO 取文件
  → DocRouter 判定类型（T-C1）
  → [DOCX] python-docx │ [PDF原生] pdfplumber │ [扫描件] pypdfium2 渲染 → OCR
  → LayoutAnalyzer 版面区块（T-C3）
  → NodeExtractor 要素 + 条款 AST（T-C4）
  → BBoxAligner 建立 offset→BBox 索引（T-C5）
  → 印章检测（T-C6，仅扫描件/图片）
  → 落库（Postgres）+ 推送 SSE 进度
  → 更新 Contract.status = PENDING_REVIEW
```

**注意**：
- ✅ 检索索引（Milvus / BM25）的写入**是否放在本任务**需要决策：
  - 若放这里 → 解析完即可检索，链路短
  - 若单独一个 `index_doc` 任务 → 解耦更好，重跑索引不影响解析
  - **建议解耦**（通过 Celery `chain` 串联），因为**换 embedding 模型时要重建索引，不希望重跑 OCR**
- ⚠️ `NotImplementedError` 换成真实实现后，**记得把 `parse_doc_task` 变成带 `bind=True` 的可重试任务**

---

## 5. WS-D · 前端配套（与三条线并行，0.5 人日，但**不做的代价很大**）

> 🔴 **赛题要求「报告含原文位置」，而前端目前没有 PDF 渲染底座**（分析报告 F9）。
> 契约层的 BBox 一路贯穿到 `RetrievedDoc.bbox`，**却没有人渲染它** —— 这是「最后一公里」。

| 编号 | 任务 | 落点 | 说明 |
|---|---|---|---|
| **T-D1** | 加 `pdfjs-dist` 依赖 + 实现 PDF 渲染 + BBox 高亮 Overlay | `apps/web-ui/package.json`、`src/components/pdf-viewer/PdfViewer.tsx` | Canvas 渲染页面 → 绝对定位 div 按 BBox 画框 → 点击风险项滚动定位 |
| **T-D2** | 风险卡片 ← 真实 API（含 CoT + 溯源） | `src/components/audit/RiskCard.tsx` | `EnhancedRiskItem.reasoningSteps` / `citations` 已在前端类型里 |
| **T-D3** | Diff 对照器 ← `RewriteResponse.diff` | `src/components/audit/DiffView.tsx` | 复用 `RewriteResponse` 契约 |
| **T-D4** | 启用 `client.ts` 里的 Zod 断言钩子 | `src/api/client.ts` | TODO 已留好位；用 `generated/zod.ts` 绑定端点 |

> 💡 **BBox 高亮的一个坑**：PDF.js 的 Canvas 渲染尺寸与 PDF 用户空间（72dpi）不同，
> 需要先取 `viewport = page.getViewport({ scale })`，再用 `viewport.convertToViewportPoint(x, y)` 做换算。
> **不要直接拿 PDF_POINTS 去画 CSS 像素** —— 缩放不平移会全错位。

---

## 6. 任务总表（可作为看板直接导入）

**优先级**：P0 = 阻塞主线 / P1 = 影响达标 / P2 = 可延后

| ID | 任务 | 工作流 | 优先级 | 人日 | 依赖 | 关键交付物 |
|---|---|---|---|---|---|---|
| T-A0-1 | 装 celery，解除 worker 不可导入 | A | **P0** | 0.2 | — | `worker.main` 可 import |
| T-A0-2 | `SparseHit` 加 `payload`，恢复 BM25 独占命中 | A | **P0** | 0.3 | — | 稀疏路独占候选不再丢弃 |
| T-A0-3 | 补 `ai-engine` workspace 依赖 | A/B | **P0** | 0.2 | — | `import ai_engine, rag_core` OK |
| T-A0-4 | 根 `pytest.ini` + 工具链登记补齐 | A | P1 | 0.3 | — | 一条命令跑全测 |
| **T-A1** | **实现 `MilvusVectorStore`** | A | **P0** | 1.5 | D1 决策 | `adapters/milvus_store.py` |
| T-A2 | config + factory 接线 | A | **P0** | 0.5 | T-A1 | `CG_RAG_MILVUS_*` 可用 |
| T-A3 | Milvus 容器编排（etcd + milvus） | A | **P0** | 0.5 | — | compose 起得来 |
| T-A4 | Milvus 集成测试（离线自动跳过） | A | P1 | 0.5 | T-A1/A3 | `test_milvus_store.py` |
| **T-A5** | **接入 embedding HTTP 服务** | A | **P0** | 0.5 | D4 | 语义向量生效（**达标前提**） |
| **T-A6** | **接入 HTTP reranker** | A | **P0** | 0.5 | D4 | 精排生效（**达标前提**） |
| T-A7 | 8 个失效配置项接线 | A | P1 | 0.5 | — | 三道防线有引擎 |
| T-A8 | 评测集 + Recall@5 脚本 | A | **P0** | 1.0 | T-A5/A6 | `eval_recall.py`（**度量工具**） |
| T-A9 | 层级切片 `SectionAwareChunker` | A | P2 | 1.5 | — | 输出契约不变 |
| T-B0 | Agent 依赖版本收口 | B | **P0** | 0.3 | D2 | 版本钉死 |
| T-B1 | LLM 适配层 `ai_engine/llm/` | B | **P0** | 0.5 | T-B0 | `get_chat_model()` |
| T-B2 | `AuditState` 状态定义 | B | **P0** | 0.5 | T-B1 | `graphs/state.py` |
| T-B3 | audit_graph 六节点 | B | **P0** | 3.0 | T-B2 | `graphs/audit_graph.py` |
| T-B4 | RAG Tool + **三道防线真实实现** | B | **P0** | 1.5 | T-B3, T-A7 | `agents/tools.py` |
| T-B5 | rewrite_graph + 分层 Memory 统一 | B | P1 | 1.5 | D2-c | `graphs/rewrite_graph.py` |
| T-B6 | Memory 持久化（Redis/PG） | B | P1 | 1.0 | T-B5 | 跨进程状态 |
| T-B7 | 4 个 Prompt 模板 | B | P1 | 0.5 | — | `prompts/templates/*.prompt` |
| T-B8 | `run_audit` 接线 + SSE | B | **P0** | 1.0 | T-B3 | 审核链路可跑 |
| T-C0 | 装 `paddlepaddle`（或 RapidOCR） | C | **P0** | 0.3 | D3 | 引擎可用 |
| T-C1 | 文档分流器 | C | **P0** | 0.5 | — | `router.py` |
| T-C2 | `OcrEngine` 适配器 | C | **P0** | 1.0 | T-C0 | 单图 OCR 输出 `OcrResult` |
| T-C3 | `LayoutAnalyzer`（规则优先） | C | P1 | 1.0 | T-C1 | `LayoutBlock` 列表 |
| T-C4 | `NodeExtractor`（要素 + 条款 AST） | C | **P0** | 1.5 | T-C3 | 要素 + `ParsedClause` |
| T-C5 | `BBoxAligner` offset↔BBox 索引 | C | **P0** | 1.0 | T-C2/C4 | **高亮闭环的关键** |
| T-C6 | 印章检测三段式 | C | P1 | 1.5 | T-C2 | `SealDetection` |
| T-C7 | `parse_doc` 接线 | C | **P0** | 1.0 | T-C1..C5 | 解析链路可跑 |
| T-D1 | PDF.js 渲染 + BBox 高亮 | D | **P0** | 1.0 | — | 赛题「原文位置」交付 |
| T-D2 | 风险卡片接真实 API | D | P1 | 0.5 | T-B8 | CoT + 溯源展示 |
| T-D3 | Diff 对照器 | D | P1 | 0.5 | T-B5 | 改写展示 |

**人日合计**：≈ **33 人日**（不含 D3/D4 的部署验证与联调缓冲）。

---

## 7. 里程碑与排期（建议 4 周）

| 周 | 里程碑 | 通关标准（**可验证**） |
|---|---|---|
| **W1** | **地基通** | ① `pytest` 一条命令 140+ passed；② `worker.main` 可导入；③ BM25 独占命中不再丢弃；④ **Milvus 容器起得来且 `pymilvus` 连得上**；⑤ **OCR 对一张合同图能输出 `OcrResult`** |
| **W2** | **语义通** | ① `MilvusVectorStore` 集成测试全绿（含 Top-10 重合度 ≥0.8）；② `CG_RAG_EMBED_MODE=http` + `RERANK_MODE=http` 生效；③ **冒烟脚本 3 条查询全部命中**（不再 `EMPTY_RECALL`）；④ `eval_recall.py` 出基线数字 |
| **W3** | **Agent 通** | ① `audit_graph` 能对一份真实合同跑完 6 阶段；② 每条风险带 `reasoningSteps` + `citations`；③ `run_audit` Celery 任务可跑并推 SSE；④ 印章检测能出 `SealDetection` |
| **W4** | **闭环通** | ① 上传 → 解析 → 审核 → 报告 全链路打通；② **前端能点风险项跳转到 PDF 对应位置并高亮**；③ 多轮改写 + Diff + 采纳成版本；④ 指标对照赛题阈值复盘 |

> ⚠️ **排期里最该保的是 W2**。「语义通」不达标，W3/W4 做再多功能都拿不到分
> （因为评分看的是分类/抽取/风险的**准确率数字**，不是功能条数）。
> **宁可砍 T-A9 / T-B6 / T-D3，也要保住 T-A5 / T-A6 / T-A8。**

---

## 8. 施工规范与红线（**违反会破坏本项目最大优势**）

### 8.1 落点表（🔴 **最重要的一节，避免被旧文档误导**）

> 本项目多份设计文档（`RAG_IMPLEMENTATION_GUIDE.md`、`AI核心开发文档.md`）规划了一个
> **已被放弃的目录结构**（`packages/rag-etl/`、`async-worker/worker/storage/clause_vector_store.py`、`worker/storage/bm25_store.py`）。
> **这些路径实际不存在，且不应被创建。** 动手前请对照此表：

| 你要做的事 | ✅ 正确落点 | ❌ 不要再建 |
|---|---|---|
| 切片 / 分块 | `packages/rag-core/rag_core/chunking/` | ~~`packages/rag-etl/`~~ |
| 向量库适配器（Milvus 等） | `packages/rag-core/rag_core/adapters/` | ~~`worker/storage/clause_vector_store.py`~~ |
| BM25 / 稀疏检索 | `packages/rag-core/rag_core/adapters/bm25_store.py` | ~~`worker/storage/bm25_store.py`~~ |
| 检索编排 | `packages/rag-core/rag_core/service.py` | 任何副本 |
| RAG 契约（模型/协议/枚举） | `packages/rag-contracts/rag_contracts/` | 任何副本 |
| Agent 图 / 节点 | `packages/ai-engine/ai_engine/graphs/` | ~~`ai_engine/agents/contract_agent.py`~~（旧文档写的，可用但需与 `graphs/` 二选一） |
| Agent 工具 | `packages/ai-engine/ai_engine/agents/tools.py` | — |
| LLM 适配 | `packages/ai-engine/ai_engine/llm/` | 在节点里直接 new 模型 |
| OCR / 版面 / 解析 / 对齐 | `packages/doc-processor/doc_processor/{ocr,layout,parser,aligner}/` | — |
| 异步任务 | `apps/async-worker/worker/tasks/` | 在 `storage/` 里写业务逻辑 |

### 8.2 三条硬红线

1. **契约层不可改**：`packages/rag-contracts/rag_contracts/` 的字段与枚举**冻结**。
   确需变更 → 先改 `RAG_MODULE_DESIGN.md` §2 + 递增 `schema_version` + 重跑 `export_schemas.py`。
   （唯一例外：T-A0-2 的 `SparseHit.payload`，因它不在 4 份导出 Schema 之列，已确认可加。）
2. **`rag-core` 不得引入重量依赖**：**禁止** `torch` / `sentence-transformers` / `jieba` / `rank_bm25`
   （`pyproject.toml` 里有明文注释）。要精排 → **走 HTTP**。
3. **生成物不可手改**：`apps/api-gateway/app/generated/schemas.py`、
   `apps/web-ui/src/api/generated/{types.ts,zod.ts}` 由 `pnpm --filter @contract-guard/schema gen` 生成。
   改了 `openapi.yaml` 就必须重跑生成并一并提交（CI 会做 diff 门禁）。

### 8.3 代码风格（**与仓库既有风格保持一致，这不是可选项**）

| 约定 | 要求 |
|---|---|
| 文件头 | 模块 docstring 用中文，首行一句话职责，随后 `from __future__ import annotations` |
| 函数 docstring | Google 风格，中文；**含 `Args:` / `Returns:` / `Raises:`**（仓库里全是这个风格） |
| 注释语言 | **中文**，且倾向解释「**为什么**」而非「是什么」（参见 `service.py` 的 clamp 注释） |
| 行宽 | 88（`ruff` 配置）；`E501` 已 ignore 但仍应对齐 |
| 导入顺序 | 标准库 → 第三方 → 本项目（`ruff` isort）；新包要登记进 `known-first-party` |
| 类型 | 全量类型标注；跨包 import 相关的新包要登记进 `mypy_path` |
| 异常 | 适配器异常统一包装为 `rag_contracts.errors` 里的错误类型（便于 `retrieve()` 降级） |

### 8.4 每个新模块必须同时交付

- [ ] **单测**（`ai-engine` / `doc-processor` 目前**零测试**，新增代码必须自带头部测试）
- [ ] **降级路径**（外部依赖不可用时不能崩，要能返回可用结果 + warning）
- [ ] **埋点**（用 `cg_observability.log_event`，带 `trace_id`）
- [ ] **`__all__` 与包出口导出**（与既有包风格一致）
- [ ] **配置项走环境变量**（`CG_` / `CG_RAG_` / `CG_LLM_` / `CG_OCR_` 前缀，禁止硬编码）

---

## 9. 验证命令清单（每个里程碑跑一次）

```powershell
# ── 基础：全量测试（W1 后应成为一条命令）
pytest -q

# ── RAG：全链路冒烟（离线，默认 inmemory + hash）
python scripts/phase1_smoke.py

# ── RAG：切到 Milvus + 真实语义服务
$env:CG_RAG_VECTOR_STORE="milvus"; $env:CG_RAG_MILVUS_URI="http://localhost:19530"
$env:CG_RAG_EMBED_MODE="http"; $env:CG_RAG_EMBED_BASE_URL="http://localhost:8080"; $env:CG_RAG_EMBED_PATH="/embed"
$env:CG_RAG_RERANK_MODE="http"; $env:CG_RAG_RERANK_HTTP_URL="http://localhost:8081"
python scripts/phase1_smoke.py
# 验收：3 条查询全部非 EMPTY_RECALL，且 top_score 明显高于 0.35

# ── RAG：Milvus 集成测试（需先设 URI，否则自动跳过）
$env:CG_RAG_TEST_MILVUS_URI="http://localhost:19530"; pytest packages/rag-core/tests/test_milvus_store.py -v

# ── RAG：召回评测（W2 必出基线数字）
python scripts/eval_recall.py --top-k 5

# ── OCR：单图 OCR 验证
python -c "from doc_processor.ocr import OcrEngine; e=OcrEngine(); print(e.ocr_page('样本.png')[:3])"

# ── Agent：单图跑通审核图（不依赖 Celery，便于调试）
python -c "from ai_engine.graphs.audit_graph import build_audit_graph; g=build_audit_graph(); print(g.invoke({'contract_id':'demo','clauses':[...]}))"

# ── 基础设施：Milvus 健康检查
curl http://localhost:9091/healthz

# ── 契约门禁（改动 openapi.yaml 后必跑）
pnpm --filter @contract-guard/schema gen
git diff --exit-code apps/web-ui/src/api/generated/types.ts apps/web-ui/src/api/generated/zod.ts apps/api-gateway/app/generated/schemas.py

# ── 代码质量
ruff check . ; mypy packages/rag-core
```

> ⚠️ **本机环境注意**（分析报告 §4.3）：若 `.venv\Scripts\python.exe` 无法启动，
> 用系统 Python + `.venv\Lib\site-packages` 前置到 `PYTHONPATH` 的方式绕过；
> Windows 下 `PYTHONPATH` 分隔符是 **`;`**（用 `:` 会 `ModuleNotFoundError`）。
> 这也是 **T-A0-4（固化 `pytest.ini`）值得早做**的原因。

---

## 10. 风险登记与回退方案

| # | 风险 | 概率 | 影响 | 应对 / 回退 |
|---|---|---|---|---|
| **R1** | **Milvus 在 Windows 上跑不起来**（Lite 不支持 / Docker 资源不足） | 中 | 高（阻塞 T-A1/A4） | 立即退到「开发用 InMemory + 演示用 Docker」；**代码侧 `MilvusVectorStore` 仍可交付**（有集成测试即可证明能力）。最坏情况保留 Qdrant |
| **R2** | **`paddlepaddle` 在 Windows 装不上** | 中 | 中（阻塞 OCR） | 切 **RapidOCR**（纯 onnxruntime）。因 `OcrEngine` 契约已冻结，**切换成本 ≈ 0.5 人日** |
| **R3** | **语义服务（embedding/rerank）没有 GPU，延迟过高** | 中 | 高（体验 + 超时） | ① 减小 `top_k` / `dense_top`；② 用更小模型（bge-small）；③ 走云端 API；④ 解析阶段预计算，检索阶段只查向量 |
| **R4** | **风险识别准确率达不到 75%**（评分关键路径） | **高** | **高** | ① **T-A8 评测脚本必须先于调参存在**；② 双路：LLM + 规则库（法规关键词命中）；③ few-shot 用真实合同样本；④ 降低单次审核范围，按条款归类后再判定 |
| **R5** | **`conflict_node` 的 N² LLM 调用导致超时/成本爆炸** | **高** | 中 | **必须「先规则粗筛再 LLM 精判」**（T-B3 已注明）；设最大候选对数上限 |
| **R6** | **Celery 多进程导致多轮改写「失忆」** | **高** | 中 | **必须用外部 checkpointer**（Postgres）+ Redis 版 `GlobalStateManager`（D2-c / T-B6） |
| **R7** | **Milvus 与 Qdrant 行为不一致导致迁移后召回变化** | 中 | 中 | T-A4 的「**Top-10 重合度 ≥0.8**」断言 + 保留 Qdrant 作对照 |
| **R8** | **旧文档误导，在 `rag-etl` 等错误路径新建平行实现** | **高** | **高**（架构分裂） | **照 §8.1 落点表施工**；动手前先 `Test-Path` 确认目标目录 |
| **R9** | **交付材料（PPT / 视频 / 演示脚本）临期突击** | **高** | 高（评审分） | 现在建 `文件集合/deliverables/` 跟踪；**每完成一个里程碑就录一段演示视频** |

---

## 11. 附：本文与既有文档的关系

| 文档 | 关系 |
|---|---|
| `项目代码深度阅读与分析报告.md` | **本文的前置事实基础**。本文所有「现状」判断引自此文 |
| `RAG_MODULE_DESIGN.md` | **架构权威，仍然有效**。唯一需变更处：§1.3 的 Milvus 结论（见 D1，建议回写） |
| `RAG搭建框架.md` / `RAG对照解析.md` | 设计论证与对照分析，**仍可参考** |
| `RAG_IMPLEMENTATION_GUIDE.md` | ⚠️ **部分路径已失效**（`rag-etl` / `worker/storage/*`）。**只参考方法论，不要照抄路径** |
| `AI核心开发文档.md` | 🟡 方法论有效（OCR→RAG→Agent 全链路）；但 §0 的落点表与 §2 的 `rag-etl` 已失效；§3 的 Graph 拓扑与本文 T-B3 一致，可交叉参考 |
| `data_scheme.md` | **契约 SSOT，仍然有效** |
| `项目进度评估报告1.1.md` | ⚠️ **部分结论已过期**（见分析报告 §7.4） |

### 最后一句建议

> 这四条（**T-A0 前置修复 → T-A5/A6 语义服务 → T-A1  Milvus 适配器 → T-C2/C5 OCR 跑通 + 坐标闭环**）
> 是全部任务里**投入产出比最高的组合**：约 5 人日，就能把项目从「架构完备、功能为零」
> 推进到「**有真实语义检索、有真实 OCR 坐标、指标可以开始量测**」。
> 其余的 Agent 编排与前端展示，都是在这个地基上「填内容」。
>
> **先把地基建好，再盖楼。**

---

## 12. 开工门禁与 plan / goal 混合编排（2026-09-21 增补）

> 本节**追加于文末**，不改动 §0–§11 的既有结论。
> 它回答一个问题：**施工文档已全部落盘，现在能不能按「Plan + Goal 混合驱动」正式开工？**
> 模式与 SOP 的**定义处**是仓库根 `AGENTS.md` §3（§3.1 模式选择 / §3.2 SOP 10 步 / §3.3 停手条件 / §3.4 汇报 / §3.5 交接），本节只做**项目级编排**，不重复定义。

### 12.1 结论：可以进入，但必须先过 Gate 0

| 判据 | 现状 | 证据 |
|---|---|---|
| 文档侧是否够用 | ✅ 够 | 模块 1 / 2 / 3 共 **27 份子任务文档**全部落盘，9 节齐；`0.施工标准与定义归属.md` 给出定义归属表 + 落点表 + DoD；两份 `AGENTS.md` 给出执行规程 |
| 每个任务能否**离线判定完成** | ✅ 能 | 每篇 §⑦ 都是可复制执行的断言；§⑧ 必测用例；基线 `pytest` **140 passed**（2026-09-21 实测，4.50s） |
| 是否会「自由发挥」跑偏 | ✅ 已被约束 | 落点表 `0.§0.8` 钉死可改文件；定义归属 `0.§0.7` 禁第二处定义；停手条件（根 `AGENTS.md` §3.3）覆盖 7 类越界 |
| 决策是否拍完 | ❌ **未拍完** | `§1 的 D2-a/b/c/d`、`§1 的 D3-a/b/c/d`、`§1 的 D4-a/b/c` 共 **12 个勾选项仍为空** |
| 依赖是否收口 | ❌ 未收口 | `celery` 不在 `.venv`（仅系统 Python 有）；`paddlepaddle` / `rapidocr-onnxruntime` **完全未装**；`pymilvus` 仅在系统 Python |
| 产品级链路是否可端到端 | ❌ 不可 | 无 DB DAL、`api-gateway` 由 `MockDataFactory` 驱动、`minio_client.upload_report` 仍是 `NotImplementedError`、模块 4 文档未落盘 |

> **一句话**：**执行的「规」已经齐了，缺的是「料」**（决策 + 依赖）。所以先花约 1 人日过 Gate 0，再进 Goal 循环。

### 12.2 Gate 0（开工前必过，≈1 人日）

| # | 事项 | 模式 | 判据（做完怎么算过） |
|---|---|---|---|
| **G0-1** | **决策收口**：D2-a/b/c/d（Agent）、D3-a/b/c/d（OCR）、D4-a/b/c（语义服务） | **Plan** | 三张决策表（`2.AGENT模块.md §2.7`、`3.OCR模块.md §3.7`、`1.RAG模块.md §1.7`）状态列改「已拍板 + 日期」；本节 §1 的 12 个勾选框全部勾上 |
| **G0-2** | **`1-1` T-A0 前置修复**：装 `celery`、补 `SparseHit.payload`、补 `ai-engine` workspace 依赖、`pytest.ini` 固化 | **Goal** | `1-1 §⑦` 断言逐条通过；此后**一条命令**即可跑测试；`1-1 §⑧` 回填表填完 |
| **G0-3** | **`3-1` T-C0 引擎与依赖安装**：`rapidocr-onnxruntime`（主）或 `paddlepaddle`（备），并让声明与实际一致 | **Default（交互）** | `3-1 §⑦` 断言通过；`scripts/check_ocr_env.py` 输出 `engine=rapid\|paddle`；`pyproject.toml` 无悬空声明、版本不敞口 |

> G0-3 建议用 **Default 模式**而不是 Goal：装包需要网络与权限，且「`paddlepaddle` 在 cp313/Windows 可能无轮子」需要人当场转向路线 B（`3-1 §⑥/§⑨`）。

### 12.3 模式分配（谁用 Plan / Goal / Default）

| 用 **Plan** | 用 **Goal** | 用 **Default** |
|---|---|---|
| 拍决策（一次只处理**一组**：D2 或 D3 或 D4） | 一个任务 ID 的实现（`1-1`、`2-4`、`3-6` …） | 前置核销、装包、只读排查 |
| 跨模块边界变更（改 `§X.8`） | 同一模块补写 / 修订 1~3 篇施工文档 | 单点修复（改一行配置 / 一个常量） |
| 要动契约层、动默认值红线 | 已拍板决策落进 L0 / L1 之外的实现 | 回填决策表状态、改文档 |

**混合回路（本项目推荐）**：

```text
Plan（出决策 + 代价） → Default（回填决策表 / 文档） → Goal（按 §3.2 十步实现）
      ▲                                                      │
      └──── Default（人裁决） ◄──── 命中 §3.3 停手条件 ────────┘
```

🔴 Goal 里**只允许**做文档 §②✅ 范围内的事；一旦命中停手条件（缺 key / 缺引擎 / 要动契约层），**退回 Default 由人裁决**，不要在 goal 里自行改口径。

### 12.4 写集冲突矩阵（**决定能不能并行**）

| 文件 / 文件集 | 会被哪些任务改 | 能否并行 |
|---|---|---|
| `packages/rag-contracts/**` | T-A0（`SparseHit.payload`）、T-C4（`patterns.py` 迁移） | 🔴 **串行** |
| `packages/rag-core/**` | T-A0（`_fuse()`）、T-A7、T-A9 | 🔴 串行 |
| `packages/doc-processor/pyproject.toml` | T-C0、T-C2、T-C4 | 🔴 串行 |
| `apps/async-worker/worker/progress.py` | T-B8（审核阶段表）、T-C7（parse 阶段表） | 🔴 **串行**（同文件不同节，先 B8 后 C7） |
| `apps/async-worker/worker/tasks/parse_doc.py` / `run_audit.py` | 各任务独占 | ✅ 可并行 |
| `packages/ai-engine/**` | T-B1 ~ T-B7（含 `agents/rewriter.py` 的同名类去重） | ⚠️ 建议**同包串行** |
| `packages/doc-processor/doc_processor/{router,ocr,parser,aligner,layout}/**` | T-C1 / T-C2 / T-C4 / T-C5 / T-C3 / T-C6 各自独占目录 | ✅ 可并行 |

> 🔴 **规则**：三条线（RAG / Agent / OCR）**可以并行**，但**同一个文件不允许两个 goal 同时改**；
> 每条线内部按各模块总纲 `§X.6 依赖图` 的顺序走。

### 12.5 建议波次（含人日）

| 波次 | 内容 | 人日 | 通关判据 |
|---|---|---|---|
| **W0** | Gate 0（§12.2） | ≈1.0 | 12 个决策勾选 + 一条命令跑测试 + 引擎自证 |
| **W1** | 三条线关键路径并行：RAG `T-A7→T-A5/A6→T-A8`；Agent `T-B0→T-B1→T-B2→T-B3`；OCR `T-C1→T-C2→T-C4→T-C5` | ≈15.6（3.5 + 6.8 + 5.3） | 各线 `§⑦` 全绿、`pytest ≥ 140 passed` 不退化 |
| **W2** | 接线收口：`T-B4`（依赖 T-A7）、`T-B8`、`T-C7`、`T-B5/B6/B7` | ≈4.0 | worker 内「解析 → 审核」可串起来（结果信封 + `data/parse/` 工件） |
| **W3** | 可扩展与增强：`T-A1~T-A4`（Milvus 支线）、`T-A9`、`T-C3`、`T-C6` | ≈8.0 | Milvus 支线过集成断言；印章 / 版面有回填数字 |
| **W4** | 前端配套（模块 4） | 待估 | **需先补 `4.前端配套模块.md`** —— 当前无任务可领 |

### 12.6 未收口项（人必须知道，不许在 goal 里"顺手解决"）

| # | 缺口 | 影响 | 处置 |
|---|---|---|---|
| R1 | 无 DB DAL；`api-gateway` mock 驱动 | `Contract.status`、SSE、真实落库**做不到** | 待建任务（`2-6 §⑨-8` / `3-6 §⑨-8` 已登记），**不在任何现有任务内** |
| R2 | `CG_LLM_*` 与 API Key 未定（D2-d） | `T-B3` / `T-B4` 只能用**桩模型**验收，真实质量无法判定 | G0-1 拍板 |
| R3 | 语义服务未定（D4） | 默认 `HashEmbedder` + `NoopReranker` → 召回率上不去，`T-A8` 基线会难看 | G0-1 拍板 + `1-6` 实现 |
| R4 | `celery` 不在 `.venv` | 依赖未收口；换机器/换容器可能直接 import 失败 | G0-2（`1-1`） |
| R5 | `paddlepaddle` / `rapidocr-onnxruntime` 未装 | OCR 引擎相关断言（`3-1` / `3-3` / `3-6` 扫描件支路）无法执行 | G0-3（`3-1`） |
| R6 | 模块 4 文档未落盘 | 前端侧无任务可领，端到端演示只有后端一半 | 需新增一次「补模块 4 文档」的 Goal |

### 12.7 下一步（立刻可执行）

1. **开一个 Plan 会话**（G0-1）：把 D2 / D3 / D4 三组决策一次拍完 —— 三份模块总纲的推荐方案已经写好，多数只需**确认**；
2. 用 **Default** 把结论回填到三张决策表 + 勾上本节 §1 的 12 个复选框；
3. 开**第一个 Goal**（G0-2，`1-1` T-A0），objective 直接复制根 `AGENTS.md` §3.1 的模板；
4. G0-3（`3-1`）穿插用 Default 做掉，遇到 `paddlepaddle` 装不上就按 `3-1 §⑨-1` 切 RapidOCR。

> **判据一句话**：Gate 0 过完之后，「一条命令跑测试 + 引擎可用 + 12 个决策已定」，
> 后面的每一个 Goal 就都变成「读文档 → 改落点 → 跑断言 → 回填」的确定性动作。
