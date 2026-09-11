# 核心构建 PRD —— 基于大模型的企业合同智能审核与风险预警系统

> **文档版本**：v1.0　**日期**：2026-08-25　**作者**：交付总监（SoftwareCompany 工作流）
> **关联文档**：`data_scheme.md`（数据类型契约 v1.0）、`architecture_assembly_report_2026-08-18.md`（Monorepo 骨架 v1.1）、`UI设计.md`、`前端开发指导契约.md`
> **赛题**：【A24】基于大模型的企业合同智能审核与风险预警系统（网新恒天）

---

## 0. 文档目的与范围

本 PRD 在既有 SSOT 契约与 Monorepo 骨架基础上，**锁定技术选型、落地路径与文件边界**，回答四个核心问题：

1. **架构怎么选、为什么选**（OCR / LLM / Agent / RAG / 前后端 / 存储逐项决策）。
2. **需求如何用技术落地、是否可行**（对照赛题量化指标做可达性分析）。
3. **关键节点与里程碑**（Phase 0–8 甘特式推进）。
4. **新增文件树与功能解释**（明确"新增/强化"模块边界）。

> **前端框架对齐说明**：赛题允许 Vue/React。既有 `apps/web-ui` 骨架已采用 **React 18 + Element Plus + Vite + TS + Zustand + Zod**；本 PRD 沿用该栈以保证骨架复用，原 `data_scheme.md` 中"Vue3"表述以本决策为准。

---

## 1. 产品概述与背景

### 1.1 命题与背景
- **命题方**：浙江网新恒天软件有限公司（CMMI5、ISO27001/9001/20000，聚焦智慧金融/能源/数字企业）。
- **命题方向**：政企服务 / 应用类。
- **核心痛点**：合同数量激增、类型多样、人工审核周期长、成本高、易遗漏；传统逐条比对效率低。

### 1.2 目标用户与场景
| 角色 | 诉求 |
|---|---|
| 企业法务 / 风控 | 批量审核采购/销售/保密/外包/劳动合同；快速定位风险、拿修改建议 |
| 法务管理者 | 维护企业标准条款库、配置风险规则与 Prompt 角色 |
| 法务主管 | 审核标注反馈、驱动数据飞轮优化模型 |

### 1.3 产品定位
"人机协同"的合同审核中枢：合同上传 → 类型识别 → 要素抽取 → 风险分析 → 标准比对 → 报告生成全流程自动化，并以**多轮对话 Agent + 分层 Memory** 支撑交互式条款改写与版本回溯。

### 1.4 核心价值主张
- **效率**：OCR+LLM 自动化替代逐条人工阅读。
- **准确**：标准条款库比对 + 跨条款矛盾检测降低漏判。
- **可解释**：CoT 推理链 + RAG 法条/标准条款精确溯源。
- **可配置**：Prompt 角色、风险规则、抽取要素、标准模板均运行时可配。

---

## 2. 需求分析

### 2.1 功能需求（映射赛题任务清单五大模块）

| 模块 | 功能点 | 赛题对应 | 本方案实现 |
|---|---|---|---|
| ① 文档解析 | 上传 .docx/.pdf/图片；OCR；保留段落结构与坐标 | 任务清单(1) | `doc-processor`：pdfplumber/python-docx 抽取 + PaddleOCR 扫描件识别 + PP-StructureV2 版面还原 |
| ② 分类与要素抽取 | ≥5 类合同分类（≥85%）；抽取双方/日期/金额/期限/管辖（F1≥80%） | 任务清单(2) | LLM 结构化抽取 + Pydantic 校验，结果绑定 `BBox` |
| ③ 风险审核 | ≥10 类风险（召回&精准≥75%）；分级高/中/低；给修改建议 | 任务清单(3) | 规则库 + LLM+Prompt 双路；`EnhancedRiskItem`（含 CoT/溯源） |
| ④ 比对与报告 | 加载企业标准模板（可配）；图文报告（HTML/PDF） | 任务清单(4) | RAG 相似度比对 + 缺失检测；Jinja2+Puppeteer 生成 A4 PDF |
| ⑤ 反馈标注 | 正确/错误标注与修改，驱动优化 | 任务清单(5) | `FeedbackItem` 飞轮：审核通过即纳入规则库/Prompt Example |

### 2.2 非功能需求与量化指标（可达性见 §4.5）

| 指标 | 赛题门槛 | 本方案目标 |
|---|---|---|
| 合同分类准确率 | ≥85% | ≥90%（LLM few-shot + 标准模板先验） |
| 要素抽取 F1 | ≥80% | ≥85% |
| 风险识别 召回&精准 | ≥75% | ≥80% |
| 报告生成 | 结构化含原文位置/等级/建议 | 满足 + 版本控制 |
| 并发 / 异步 | — | Celery 任务队列，长耗时 taskId+SSE |
| 可解释 / 可配置 | 必须 | CoT + 溯源 + 4 类配置中心 |

### 2.3 创新需求（赛题"其他"鼓励项）
1. **多轮对话式合同修改**（"将违约责任上限改为 20%"）→ LangGraph + 分层 Memory。
2. **知识图谱辅助跨条款关联分析** → `conflict.py` 跨条款矛盾 + 可选 Neo4j 条款关系图。
3. **扫描件印章/签名定位与合规审查** → PaddleOCR 印章类检测 + `SealSignatureDetection`。

### 2.4 数据契约需求
严格遵循 `data_scheme.md` 四大补齐维度（印章合规 / 跨条款矛盾 / 系统可配置 / CoT+RAG 溯源）与六大基础契约（类型边界、命名、异常幂等、前后端联通、AI-Coding 防御、联调测试）。本 PRD 所有模块输出必须收敛到该 SSOT。

---

## 3. 总体架构设计

### 3.1 架构选型总览与依据

| 层 | 选型 | 决策依据 |
|---|---|---|
| **OCR 引擎** | **PaddleOCR（GitHub 开源）+ PP-StructureV2** | 中文合同识别 SOTA；自带版面分析（表格/标题/印章类）；支持 CPU 推理，契合"低配置"；替代赛题建议的 Tesseract 以保精度 |
| **LLM 大脑** | **云端 API（API Key 接入）**：DeepSeek / 通义千问 Qwen / OpenAI 多模型兜底 | 零本地 GPU、按量计费、迭代快；赛题允许商用 API 并需说明可行性（§4.2）；直接满足"低配置运行" |
| **Agent 编排** | **LangGraph** | 显式状态图 + 条件边 + Checkpointer，天然支撑多轮对话、分层 Memory、人工介入（human-in-the-loop）与可观测 |
| **RAG 系统** | **传统 RAG + 开源框架（LlamaIndex 编排）+ ELT 管线** | LlamaIndex 成熟 Node/Chunk/Retriever；自建 ELT 做清洗；向量库用 **Qdrant**（与既有 docker-compose 一致） |
| **后端** | **FastAPI + pydantic-settings + Celery** | 异步任务隔离 OCR/LLM 长耗时；SSOT 生成 Pydantic DTO |
| **前端** | **React 18 + Element Plus + Vite + TS + Zustand + Zod** | 复用已装配骨架；`@/api/client` 唯一 HTTP 入口 + Zod 断言 |
| **存储** | **PostgreSQL（业务/全局状态）+ Qdrant（向量）+ MinIO（文件）+ Redis（队列/短期记忆）** | 职责分离；MinIO 实现 presign 直传三步法 |

### 3.2 技术选型对比决策表（OCR / LLM / 向量库）

| 候选 | 胜出 | 落选 | 理由 |
|---|---|---|---|
| OCR：PaddleOCR vs Tesseract vs Marker | PaddleOCR | Tesseract（中文弱）、Marker（偏原生PDF） | 中文+印章+版面综合最强 |
| LLM：云端API vs 本地微调 | 云端API | 本地（需GPU、周期长） | 赛题初赛重完成度与可行性，云端最快达标 |
| 向量库：Qdrant vs pgvector vs Milvus | Qdrant | pgvector(规模弱)、Milvus(重) | 已入 docker-compose，轻量易部署 |

### 3.3 分层架构图

```
┌──────────────────────────────────────────────────────────────────────┐
│                          前端 Web-UI (React+EP)                         │
│  Dashboard / Audit双栏 / Settings规则 / Report / Feedback             │
└───────────────────────────────┬──────────────────────────────────────┘
                                 │  @/api/client (X-Request-ID, Zod)
┌───────────────────────────────▼──────────────────────────────────────┐
│                       API Gateway (FastAPI)                            │
│  路由 / 幂等中间件 / 统一包络 / 鉴权                                    │
└───────┬───────────────────────┬───────────────────────┬──────────────┘
        │ 提交任务               │ 同步查询               │ 文件 presign
┌───────▼────────┐    ┌─────────▼─────────┐    ┌─────────▼─────────┐
│ Async Worker   │    │  AI Engine        │    │  Storage          │
│ (Celery)       │◄───│  (LangGraph)      │    │ Postgres/Qdrant/  │
│ parse_doc      │    │  classify→extract │    │ MinIO/Redis       │
│ run_audit      │    │  →risk→compare    │    │                   │
│ gen_report     │    │  →rewrite(Agent)  │    │                   │
└───────┬────────┘    └─────────┬─────────┘    └─────────┬─────────┘
        │                       │                        │
┌───────▼────────┐    ┌─────────▼─────────┐    ┌─────────▼─────────┐
│ doc-processor  │    │  RAG (ELT+检索)    │    │  LLM Cloud API    │
│ PaddleOCR      │    │  Extract→Load→    │    │  DeepSeek/Qwen/   │
│ PP-Structure   │    │  Transform→检索    │    │  OpenAI (Key)     │
└────────────────┘    └───────────────────┘    └───────────────────┘
```

### 3.4 核心数据流时序（Agent 状态机）
```
upload(presign→直传→触发)
  → parse_doc(OCR/PDF解析, 输出 文本+AST+BBox)
  → LangGraph: START→classify→extract→risk_detect→conflict_detect→standard_compare→report_draft→END
  → 每节点写 Postgres + 推送进度(SSE)
  → 用户进入 Audit 页: 触发 rewrite Agent(多轮, 分层Memory) → commit(新版本)
  → feedback → 数据飞轮
```

---

## 4. 技术落地方案与可行性分析

### 4.1 OCR 引擎落地（GitHub 优质项目直接接入）
- **接入方式**：以 `doc-processor/ocr/` 为适配层，封装 PaddleOCR（PP-Inference 推理，CPU 可用）。原生数字 PDF 走 pdfplumber 直抽（带坐标），扫描件/图片走 PaddleOCR + PP-StructureV2 版面还原。
- **印章/签名定位**：启用 PP-Structure 的 `seal` 检测类 + 自定义印章检测头，输出 `SealSignatureDetection`（type/status/bbox/extractedText/isMatchedWithParties/complianceNote）。
- **BBox 对齐**：`aligner/` 将各来源坐标归一化为 `PDF_POINTS`（见 data_scheme §1.3），保证左栏 Canvas 高亮与右栏风险卡片联动。

### 4.2 LLM 大脑落地（云端 API + Key 接入）
- **接入方式**：`ai_engine/llm/` 提供统一 Provider Adapter（OpenAI-compatible 协议），配置 `LLM_PROVIDER`、`API_KEY`、`BASE_URL`、`MODEL`。
- **多模型兜底**：主用 DeepSeek / Qwen（中文合同强、成本低），异常自动切换 OpenAI；温度/角色由 `PromptRoleConfig` 驱动（STRICT/BALANCED/LOOSE）。
- **可行性**：纯 API 调用，本地仅需联网与基础算力跑 OCR（CPU 即可）；成本按 token 计量，初赛演示量级极低；满足赛题"低配置运行说明"。
- **合规**：API Key 仅存服务端环境变量，不落前端；合同文本脱敏后调用。

### 4.3 LangGraph Agent 编排
- **状态图（StateGraph）**：
  - 节点：`classify` / `extract` / `risk_detect` / `conflict_detect` / `standard_compare` / `report_draft` / `rewrite`（交互节点，human-in-the-loop）。
  - 边：条件边按 `ContractStatus` 与置信度分流（低置信→人工复核）。
  - Checkpointer：持久化到 Postgres，支撑断点续跑与多轮。
- **分层 Memory（防上下文超载）**：
  - `GlobalContractState`：合同元数据 + 全局 AST 拓扑 + 当前版本节点（Postgres）。
  - `ScopedClauseMemory`：仅当前条款近 3–5 轮对话（Redis，TTL 滑动窗口）。
  - `RagRuleContext`：动态检索的标准条款 + 法条（每次请求按需注入，不全量重载）。
- **交互改写**：`rewrite` 节点接收 `AgentRewriteRequest`（含 `scopedMemory`），返回 `RewriteResponse`（before/after/Diff/CoT/citations），commit 后生成新版本（v1.0→v1.1）并支持回溯。

### 4.4 RAG 系统与 ELT 管线
- **ELT 管线（自建 Python 包 `packages/rag-etl`）**：
  - **Extract**：抓取企业标准 Word 范本、法条库（《民法典》等）、裁判文书，落原始层。
  - **Load**：写入 staging（Postgres `raw_docs`）。
  - **Transform**：切片（按条款/语义窗口）、元信息标注、调用 Embedding API 向量化。
  - 调度：Celery 定时/手动触发，保证知识库可增量更新。
- **检索（LlamaIndex 编排 + Qdrant）**：Dense（向量）+ Sparse（BM25）混合检索 → Cross-Encoder 重排 → Top-K 注入 Prompt。
- **跨条款矛盾**：检索相关条款簇 → LLM 比对数值/责任/期限一致性 → 产出 `CrossClauseConflict`。
- **缺失检测**：标准模板条款向量比对合同，未命中即 `MissingClause`（含补全建议与法条依据）。

### 4.5 可行性分析与量化指标可达性
- **准确率可达性**：LLM 结构化抽取 + 标准模板先验 + few-shot，对规范中文合同分类≥90%、要素 F1≥85% 可达；风险识别靠"规则库(确定性)+LLM(泛化)"双路，召回/精准≥80% 可达。
- **成本/资源**：OCR CPU 推理 + LLM 云端 API，单合同成本约角级；初赛演示无需 GPU。
- **风险与缓解**：
  | 风险 | 缓解 |
  |---|---|
  | OCR 印章误检 | 人工复核开关 + 置信度阈值 |
  | LLM 幻觉 | CoT+溯源+RAG 约束 + 人工 in-the-loop |
  | API 限流/超时 | 多模型兜底 + 重试 + 任务队列削峰 |
  | 长文本上下文溢出 | 分层 Memory + 按条款切片 |

---

## 5. 关键节点与里程碑（落地路径）

| Phase | 目标 | 关键交付 | 周期(估) |
|---|---|---|---|
| **P0** 环境基线 | 打通 SSOT 生成管道 + docker 一键起 | openapi→TS/Zod/Pydantic 生成；compose 起服务 | 3d |
| **P1** OCR+解析 MVP | 文档上传→文本+AST+BBox | doc-processor 接入 PaddleOCR；presign 三步法 | 5d |
| **P2** 分类+要素 | 类型识别 + 5 要素结构化 | LLM Adapter + extract 节点 + 评估脚本 | 4d |
| **P3** 风险识别分级 | ≥10 类风险 + 等级 + 建议 | 规则库 + risk_detect 节点 + EnhancedRiskItem | 5d |
| **P4** RAG 比对+缺失 | 标准比对 + 缺失检测 + 跨条款 | rag-etl + retriever/reranker + conflict | 6d |
| **P5** 报告+印章合规 | A4 报告 + 印章签名审查 | report_draft + gen_report + SealDetection | 4d |
| **P6** Agent 多轮+Memory | 对话改写 + 版本回溯 | LangGraph rewrite + 分层 Memory | 5d |
| **P7** 前端 5 页+飞轮 | 双栏联动 + 反馈闭环 | web-ui 5 views + feedback 飞轮 | 7d |
| **P8** 联调+演示+材料 | 全链路 + 提交包 | 冒烟用例 + 演示视频 + 文档 | 5d |

> 关键路径：**P0 → P1 → P2 → P3 → P4 → P6 → P7 → P8**；P5 可与 P3/P4 并行。

---

## 6. 开发指南与落地步骤方案

### 6.1 环境与依赖
- Python 3.11 + `uv`（后端/AI 工作区）；Node 20 + `pnpm`（前端）。
- 关键依赖：`fastapi`、`celery[redis]`、`langgraph`、`llama-index`、`qdrant-client`、`paddleocr`、`pdfplumber`、`python-docx`、`pydantic-settings`、`openai`（适配层）。
- 服务：`postgres`(pgvector 可选)、`qdrant`、`redis`、`minio`。

### 6.2 新增文件树状图与功能解释

> 说明：以下树在既有 Monorepo 骨架（`Serve_bage/contract-guard-ai`）基础上，**★ 标记为本 PRD 新增/强化的关键模块**。

```
contract-guard-ai/
├─ packages/
│  ├─ schema/                      # SSOT：openapi.yaml + 生成脚本(gen-ts/gen-zod/gen-py)
│  ├─ doc-processor/               # 文档解析引擎
│  │  ├─ ocr/                      # ★ PaddleOCR 适配层 + 印章检测封装
│  │  ├─ layout/                   # PP-StructureV2 版面还原
│  │  ├─ parser/                   # pdfplumber / python-docx 节点提取
│  │  └─ aligner/                  # BBox 归一化(PDF_POINTS)
│  ├─ ai-engine/                   # Agent 逻辑
│  │  ├─ graphs/                   # ★ LangGraph 状态图定义(audit_graph.py/rewrite_graph.py)
│  │  ├─ llm/                      # ★ 云端API Provider Adapter(deepseek/qwen/openai)
│  │  ├─ agents/                   # auditor/rewriter/conflict（节点实现）
│  │  ├─ memory/                   # global_state / scoped_memory（分层Memory）
│  │  ├─ rag/                      # retriever / reranker
│  │  └─ prompts/                  # Jinja 模版加载(PromptRoleConfig 驱动)
│  ├─ rag-etl/                     # ★ 新增：ELT 数据清洗管线(extract/load/transform)
│  │  ├─ extractors/               # 标准范本/法条/裁判文书抓取
│  │  ├─ transformers/             # 切片/标注/向量化
│  │  └─ pipelines/                # Celery 调度编排
│  └─ shared/                      # 公共类型/常量(引用 generated)
├─ apps/
│  ├─ api-gateway/                 # FastAPI：路由/幂等/统一包络
│  ├─ async-worker/                # Celery：parse_doc/run_audit/gen_report
│  └─ web-ui/                      # React18+EP：5 大页面
│     └─ src/views/                # Dashboard/Audit/Report/Settings/Feedback
├─ docker/                         # compose + Dockerfile + nginx
└─ deploy/                         # 低配置运行说明(CPU+OCR+云端LLM)
```

**★ 新增模块功能解释**
- `doc-processor/ocr`：**直接落地 GitHub 优质 OCR（PaddleOCR）**，对外暴露 `recognize(image/bytes)->Text+BBox`，屏蔽推理细节；印章检测复用 PP 模型输出 `SealSignatureDetection`。
- `ai-engine/graphs`：**LangGraph 编排核心**，把"分类→抽取→风险→矛盾→比对→报告"固化为可观测状态图；`rewrite_graph` 支撑多轮对话改写与 human-in-the-loop。
- `ai-engine/llm`：**云端 LLM 统一适配层**，以 OpenAI-compatible 协议归一 DeepSeek/Qwen/OpenAI，集中管理 Key/BaseURL/Model/温度，支撑 `PromptRoleConfig` 动态切换。
- `rag-etl`：**传统 RAG 的 ELT 清洗管线**，独立于推理链路，负责把企业标准库/法条加工成可检索向量，保证知识库增量更新与质量。

### 6.3 关键开发步骤（示例）
1. **OCR 接入**：`pip install paddleocr` → 在 `ocr/adapter.py` 封装 `PaddleOCR()` → 输出 `(text, bbox, page)` → 交由 `aligner` 归一。
2. **LLM 适配**：实现 `LLMClient.chat(system, user, temperature)`；在 `config.py` 读 `LLM_PROVIDER/API_KEY`；`prompts/loader` 注入 `PromptRoleConfig`。
3. **LangGraph 图**：`StateGraph(AuditState)` 添加节点与条件边 → `compile(checkpointer=PostgresSaver)` → 在 `run_audit` task 中 `agraph.invoke(input, config)`。
4. **RAG 管线**：`rag-etl` 抽取标准库 → 切片 → `QdrantClient.upsert(vectors)`；检索时在 `risk_detect` 前调用 `retriever.search(query)` 注入上下文。
5. **接口与契约**：所有 DTO 由 `schema/openapi.yaml` 生成；新增端点先改 YAML 再 `pnpm gen:*`。

### 6.4 数据契约接入
- 全链路类型以 `data_scheme.md` 为准：ID 为 string、金额 `Money`、百分比 `[0,1]`、时间 ISO8601 UTC、集合空为 `[]`、业务错误 `code` 枚举、写操作 `X-Request-ID` 幂等 + `version` 乐观锁。
- 四大补齐维度（`SealSignatureDetection` / `CrossClauseConflict` / `SystemAuditConfiguration` / `EnhancedRiskItem`+CoT/溯源）必须实现且附冒烟用例。

### 6.5 测试联调
- 基于 Schema 动态 Mock（Prism/openapi-faker）；每接口 ≥2 组 JSON（成功/异常）。
- 评估脚本：`scripts/eval.py` 用 CUAD/脱敏样例计算分类准确率、要素 F1、风险召回/精准。
- 前端状态严格对应四态矩阵（Loading/Empty/Success/Error）。

### 6.6 部署
- `docker-compose up` 一键起 API/Worker/PG/Qdrant/Redis/MinIO/Web/Nginx。
- **低配置说明**：OCR 用 CPU 推理；LLM 走云端 API 无需本地 GPU；`deploy/low-spec.md` 给出 2C4G 单机部署指引。

---

## 7. 提交材料映射（对照赛题交付清单）

| 赛题要求材料 | 本方案对应交付 |
|---|---|
| (1) 项目概要介绍 | 本文 §1 + 独立一页概要 |
| (2) 项目简介 PPT | 基于本文提炼 10–15 页 |
| (3) 项目详细方案 | 本文全篇 + `data_scheme.md` + 架构报告 |
| (4) 项目演示视频 | P7 完成后录制端到端流程 |
| (5)① 使用说明文档 | `deploy/` + README |
| (5)② 训练/微调记录与数据说明 | `rag-etl` 数据说明 + LLM Prompt 迭代记录（无微调，云端 API） |
| (5)③ 分工与开发过程记录 | Phase 看板 + 本 PRD 里程碑 |
| (6) 补充材料 | 评估脚本结果、CoT 溯源样例、印章合规 demo |

---

## 8. 风险与质量保障

- **质量门禁**：CI 跑 Spectral Lint（camelCase/布尔前缀/ID string/集合禁 null）+ 生成产物比对 + Python 编译 + 前端类型检查。
- **可解释性保障**：每条风险/改写必带 `reasoningSteps` 与 `citations`，前端显式渲染溯源卡片。
- **数据飞轮**：`FeedbackItem` 审核通过后自动回流到规则库/Prompt Example，形成闭环优化。
- **回滚与版本**：合同每次采纳修改生成新版本（`version` + `currentVersionId`），支持版本回溯对照。

---

## 9. 自检清单（PRD 完整性）

- [ ] 架构选型及依据逐项给出（OCR/LLM/Agent/RAG/前后端/存储）
- [ ] 需求映射到赛题五大模块 + 量化指标
- [ ] 三大创新点（多轮对话/知识图谱跨条款/印章合规）均已落地设计
- [ ] LangGraph / 云端 LLM / PaddleOCR / RAG-ELT 落地步骤明确
- [ ] 可行性分析含量化可达性与风险缓解
- [ ] Phase 0–8 里程碑 + 关键路径清晰
- [ ] 新增文件树 + ★模块功能解释完整
- [ ] 数据契约接入指向 data_scheme.md 四大维度
- [ ] 提交材料映射赛题清单
