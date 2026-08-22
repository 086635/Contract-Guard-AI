# 项目主体架构落盘交付报告

> 文档版本：v1.1　日期：2026-08-18（补充第 9 节「规划与推进路线图」）　作者：交付总监（SoftwareCompany 工作流）
> 配套 SSOT：`文件集合/report/data_scheme.md`（数据类型契约规范 v1.0）
> 落盘根目录：`E:\important_project\Serve_bage`（即 `contract-guard-ai` 单仓根，直接落于 `Serve_bage` 下）

**执行摘要**：在 `Serve_bage` 根目录从零组装了完整 Polyglot Monorepo 骨架（113 文件，蓝图关键叶子 100% 覆盖），SSOT 权威 `openapi.yaml`（OpenAPI 3.1.0）与 45 个 Python 文件均通过实跑校验。当前为"装配完成、逻辑待实现"状态；下一步以 Phase 0（打通 SSOT 生成管道）为关键路径起点，按标准 SOP 推进。

---

## 1. 背景与目标

依据 `data_scheme.md` 的工程约束与既定 Polyglot Monorepo 蓝图，在 `Serve_bage` 根目录组装企业合同智能审核系统的**多语言单仓骨架**，满足：

- **单一真理源（SSOT）代码生成管道**：以 `packages/schema/openapi.yaml` 为权威，前端 TS/Zod、后端 Pydantic DTO 一律由 CLI 生成。
- **前后端分离 + AI Agent 解耦 + 异步 Task 隔离**：前端 SPA、后端 API、AI 引擎、异步 Worker 各自独立包/应用。
- **包管理**：前端 `pnpm workspace`；后端/AI 引擎 `uv`（Python）workspace。

> **工作流判定**：本任务属于"按蓝图装配"——架构方案与 SSOT 规范已被完全锁定，故由交付总监直接执行骨架组装，未触发 PRD→架构→编码→测试的完整 SOP。

> **重要说明**：本次为**从零重建**。上一次会话的落盘结果未持久化到本工作区（仅 `文件集合/report/data_scheme.md` 留存），本次重新创建并已完成校验。

---

## 2. 落盘前现状盘点

| 项目 | 状态 |
|---|---|
| `文件集合/report/data_scheme.md` | ✅ 已存在（SSOT 参考规范） |
| 此前会话的 monorepo 文件（packages/apps/docker/…） | ❌ 未持久化（本次重建） |
| 零散 memory 文件 | 发现误置于 `文件集合/report/.workbuddy/...`，已迁移至 `.workbuddy/memory/2026-08-18.md` |

---

## 3. 本次落盘文件清单（按 9 个分区）

> 共落盘 **113 个文件/占位**（含 `__init__.py` / `.gitkeep` 占位）；其中**内容文件约 80+ 个**。关键叶子文件 100% 覆盖蓝图。

### 3.1 根级配置与 CI（6）
- `pnpm-workspace.yaml` —— 前端/packages 工作区定义
- `pyproject.toml` —— Python（uv）工作区根（`members = ["packages/*","apps/*"]`）
- `README.md` —— 开发/运行/CI 说明
- `.gitignore` —— 含生成产物红线（generated 三件套）
- `.github/workflows/schema-check.yml` —— Spectral 校验 + CodeGen 产物对比门禁
- `.github/workflows/deploy.yml` —— Docker 镜像打包与部署

### 3.2 packages/schema（SSOT 核心，7）
- `openapi.yaml` —— **权威规范（OpenAPI 3.1.0）**，含 12 个 `components.responses` + 全量 domain model + 端点，已通过 YAML 解析与 `$ref` 解析校验
- `spectral.yaml` —— Lint 规则（camelCase / 布尔前缀 / ID 为 string / 集合禁 null / 成功包络）
- `package.json` —— 触发代码生成的 npm scripts（`gen:ts`/`gen:zod`/`gen:py`/`lint`）
- `scripts/gen-ts.ts` —— openapi-typescript 生成 TS 类型
- `scripts/gen-zod.ts` —— openapi-zod 生成运行时 Schema
- `scripts/gen-py.sh` —— datamodel-code-generator 生成 Pydantic DTO

### 3.3 packages/ai-engine（AI Agent 逻辑包，14）
- `pyproject.toml` + `ai_engine/__init__.py`
- `agents/`：`auditor.py`（风险审核+CoT+溯源）、`rewriter.py`（条款重写+Diff+分层 Memory）、`conflict.py`（跨条款矛盾）、`__init__.py`
- `memory/`：`global_state.py`（全局 AST 拓扑）、`scoped_memory.py`（条款级滑动窗口）
- `rag/`：`retriever.py`（Dense+Sparse 混合检索）、`reranker.py`（Cross-Encoder 重排）
- `prompts/`：`loader.py`（Jinja 式模版加载器）、`__init__.py`

### 3.4 packages/doc-processor（文档解析引擎，11）
- `pyproject.toml` + `doc_processor/__init__.py`
- `layout/`（PP-StructureV2 版面分析）、`ocr/`（PaddleOCR + 印章定位）、`parser/`（pdfplumber/python-docx 节点提取）、`aligner/`（BBox 归一化对齐）各含 `__init__.py` 实现骨架

### 3.5 apps/api-gateway（FastAPI 主网关，15）
- `pyproject.toml`、`app/main.py`（装配路由+CORS+幂等中间件）、`app/config.py`（pydantic-settings，`CG_` 前缀）
- `app/api/middlewares/idempotency.py` + `__init__.py` —— `X-Request-ID` 幂等 + `X-Trace-Id` 透传（§4.2）
- `app/generated/schemas.py` —— **Auto-Gen 占位**（标注 `AUTO-GENERATED — DO NOT EDIT`）
- `app/api/v1/`：`contracts.py`、`agent.py`、`config.py`、`report.py`、`feedback.py`、`tasks.py`、`__init__.py`（路由骨架，逻辑待实现 `501`）
- `app/db/`、`app/services/`、`tests/` 占位

### 3.6 apps/async-worker（Celery 异步节点，9）
- `pyproject.toml`、`worker/main.py`（Celery 单一实例）
- `worker/tasks/`：`parse_doc.py`（解析/OCR）、`run_audit.py`（批量审核）、`gen_report.py`（报告渲染）
- `worker/storage/`：`minio_client.py`、`qdrant_client.py`

### 3.7 apps/web-ui（前端 SPA，20+）
- `package.json`（React18 + Vite + TS + Zustand + Axios + Zod）、`vite.config.ts`（`@` 别名 + `/api` 代理）、`index.html`
- `src/api/client.ts` —— **全局唯一 HTTP 客户端**，内置 `X-Request-ID` 幂等拦截 + Zod 断言拦截（§6.2/§6.4）
- `src/api/generated/{types,zod}.ts` —— **Auto-Gen 占位**
- `src/`：`main.tsx`、`App.tsx`、`router/index.tsx`（5 大页面路由）、`views/`（Dashboard/Audit/Report/Settings/Feedback）、`stores/contractStore.ts`（Zustand + AST/分层 Memory）、`components/{pdf-viewer,audit,chat}` 三个组件骨架

### 3.8 docker（部署配置，5）
- `docker-compose.yml` —— 一键启动 API/Worker/Postgres/Qdrant/Redis/MinIO/Web/Nginx
- `Dockerfile.api`、`Dockerfile.worker`（uv workspace 单体构建）、`Dockerfile.web`（前端构建 + nginx 托管）
- `nginx.conf` —— SPA history 回退 + `/api` 反代

---

## 4. 关键工程约定（已落到具体文件）

| 约定 | 来源（data_scheme.md） | 落地点 |
|---|---|---|
| SSOT 红线 | §0/§6.1 | `generated/schemas.py`、`generated/{types,zod}.ts` 标注 `AUTO-GENERATED — DO NOT EDIT`；`.gitignore` 忽略三件套 |
| 双工作区 | 蓝图 | `pnpm-workspace.yaml` + 根 `pyproject.toml`（uv workspace） |
| 统一响应包络 | §1.3/§4 | `openapi.yaml` 的 `ApiEnvelopeBase` + `ApiError`；所有 200 响应 `allOf` 包络 |
| 幂等 / 乐观锁 | §4.2/§4.3 | `idempotency.py` 中间件；`X-Request-ID` 头；`version` 字段 |
| 命名规范 | §3 | `spectral.yaml` 强制 camelCase / 布尔前缀 / ID 为 string / 集合禁 null |
| 全局唯一 HTTP 客户端 | §6.4 | `web-ui/src/api/client.ts` 单例 + 拦截器 |
| 文件直传三步法 | §5.3 | `POST /contracts/presign` → 直传 → `POST /contracts/upload` |
| 长耗时任务 | §5.4 | `GET /tasks/{taskId}` 轮询 + `GET /tasks/{taskId}/stream` SSE |

---

## 5. 校验结果

| 校验项 | 命令/方式 | 结果 |
|---|---|---|
| openapi.yaml 语法 | PyYAML `safe_load` | ✅ 解析为 OpenAPI **3.1.0** |
| 核心 schema 完整性 | 断言 10 个核心 schema 存在 | ✅ 齐全 |
| `$ref` 引用解析 | 遍历全部 `$ref` 比对 `components` | ✅ 无未解析引用（12 responses + N schemas） |
| Python 语法 | `python -m py_compile`（45 个 .py） | ✅ 全部通过 |
| 蓝图叶子文件覆盖 | 逐文件存在性比对 | ✅ 100% 覆盖 |

---

## 6. 已知缺口与待办（下阶段推进对象）

| 缺口 | 说明 | 建议归属 |
|---|---|---|
| 路由业务逻辑 | `v1/*.py` 均为 `501` 骨架（`NotImplementedError`） | 后端工程师 |
| `db` / `services` | 仅占位，缺 SQLAlchemy Model + Alembic 迁移 | 后端工程师 + 架构师 |
| SSOT 生成管道未贯通 | 尚未运行 `pnpm gen`，generated 三件套为占位 | 全员（先打通） |
| 前端页面为空壳 | views/components 为占位，未接 client | 前端工程师 |
| AI 引擎未落地 | agents/rag/memory 为骨架，未接 LLM/Qdrant | AI 工程师 |
| 蓝图偏差项 | `/feedback`、`/tasks` 端点补充了 `feedback.py`/`tasks.py`（蓝图仅列 4 路由） | 待用户拍板是否折叠进 contracts/report |
| CI 内容核对 | `schema-check.yml`/`deploy.yml` 未实跑（需 token/密钥） | QA / DevOps 专项 |

---

## 7. 下一步推进路线（建议）

1. **环境准备**：安装 pnpm + uv；`pnpm install` + `uv sync --all-packages`。
2. **打通 SSOT 管道**（关键前置）：`pnpm --filter @contract-guard/schema gen` → 产出 TS/Zod/Pydantic，消除 generated 占位。
3. **后端实现**：架构师出 `app/db` 模型与任务分解 → 工程师批量实现 v1 路由（`501` → 真实逻辑）、services、Worker 任务。
4. **AI 引擎打通**：接 LLM（LangGraph）+ Qdrant + PaddleOCR，落地 auditor/rewriter/conflict + RAG。
5. **前端实现**：基于生成类型接 `client.ts`，填充 5 大页面与组件。
6. **CI 核对**：实跑 `schema-check.yml`（Spectral + CodeGen 对比），补 deploy 密钥。

---

## 8. 附录：完整目录树

```
Serve_bage/                              （= contract-guard-ai 单仓根）
├── .github/workflows/  schema-check.yml  deploy.yml
├── packages/
│   ├── schema/        openapi.yaml  spectral.yaml  package.json
│   │                  scripts/{gen-ts.ts,gen-zod.ts,gen-py.sh}
│   ├── ai-engine/     pyproject.toml  ai_engine/{agents,memory,rag,prompts}/*
│   └── doc-processor/ pyproject.toml  doc_processor/{layout,ocr,parser,aligner}/*
├── apps/
│   ├── api-gateway/   pyproject.toml  app/{main,config}.py
│   │                  app/api/{middlewares, v1/*}  app/{db,services,generated,tests}/
│   ├── async-worker/  pyproject.toml  worker/{main.py, tasks/*, storage/*}
│   └── web-ui/        package.json  vite.config.ts  index.html
│                      src/{main.tsx,App.tsx, router/, views/*, stores/*,
│                           components/{pdf-viewer,audit,chat}/*, api/{client.ts, generated/*}}
├── docker/            docker-compose.yml  Dockerfile.{api,worker,web}  nginx.conf
├── pnpm-workspace.yaml
├── pyproject.toml
└── README.md
```

> 完整逐文件清单见本文第 3 节；逐文件树可由 `find . -path ./文件集合 -prune -o -type f -print` 复现。

---

## 9. 下一步规划与推进路线图（供决策与推动）

> 本节将第 6/7 节的"缺口"与"路线"转化为**可分派、可验收、可排期**的推进计划，直接支撑立项与团队组建决策。

### 9.1 阶段划分与关键路径（Critical Path）

| 阶段 | 目标 | 关键交付物 | 建议归属 | 验收标准 | 周期 |
|---|---|---|---|---|---|
| **Phase 0** 基础设施与管道贯通 | 消除 generated 占位、CI 门禁可跑 | `pnpm gen` 真实产出 TS/Zod/Pydantic；`uv run` API 启动无 import 错误；`schema-check.yml` 实跑通过 | 架构师 + DevOps | 本地生成类型成功、API 可 `uv run` 启动、CI 绿灯 | ~1 周 |
| **Phase 1** 数据层与核心 API | contracts 主链路端到端 | `app/db` SQLAlchemy Model + Alembic 迁移；`services/`；v1 路由 `501`→真实逻辑 | 后端工程师（架构师出分解） | 上传→解析→审核主流程可跑通 | ~2 周 |
| **Phase 2** AI 引擎落地 | 审核/重写/矛盾检测可用 | auditor/rewriter/conflict + RAG 接 LLM/Qdrant；doc-processor 接 PaddleOCR | AI 工程师 | 单条款审核 + 跨条款矛盾检测可用 | ~2–3 周 |
| **Phase 3** 前端实现 | 5 大页面可交互 | views/components 接 `client.ts`；Zustand 状态；BBox 高亮 | 前端工程师 | Dashboard/Audit/Report 主流程可交互 | ~2 周 |
| **Phase 4** 部署与加固 | 全栈一键起、可压测 | docker-compose 全栈起；`deploy.yml` 实跑；基础压测 | DevOps | `docker compose up` 起全部服务、接口连通 | ~1 周 |

**关键路径**：Phase 0 → Phase 1 →（Phase 2 ∥ Phase 3）→ Phase 4。Phase 0 是全局阻断点——generated 三件套未生成前，前端与后端 import 均会失败，故必须最先完成。

### 9.2 决策待办（需用户拍板）

- **D1 — 蓝图偏差处理**：为完整覆盖 SSOT 的 `/feedback`、`/tasks` 端点，已补 `feedback.py`/`tasks.py`；是否保留为独立路由，还是折叠进 `contracts.py`/`report.py`？
- **D2 — AI 编排框架选型**：LangGraph（有状态图、易调试）vs 原生 `asyncio` 协程（轻量、无额外依赖）？影响 `packages/ai-engine` 后续实现骨架。
- **D3 — 是否触发标准 SOP**：确认后我可立即按 `架构师→工程师→QA` 团队流水线进入 Phase 0，而非继续"交付总监装配"模式。

### 9.3 风险登记（Risk Register）

| 编号 | 风险 | 影响 | 缓解措施 |
|---|---|---|---|
| R1 | SSOT 生成管道未先打通 → generated 占位致全栈 import 失败 | 阻断性 | Phase 0 优先；CI 门禁强制 `gen` 与手改物一致 |
| R2 | PaddleOCR / PP-StructureV2 依赖重，镜像可能 >2GB | 构建慢、部署重 | 多阶段构建 + 模型懒加载 + 基础镜像预热 |
| R3 | 大模型审核成本/延迟高 | 体验与费用 | 长任务强制走异步 Worker（已隔离）；批处理 + 向量缓存 |
| R4 | OpenAPI 与生成物漂移 | 前后端契约不一致 | `schema-check.yml` 中 CodeGen 产物对比门禁（已配置，待实跑） |

### 9.4 立即可执行的第一步

1. 触发 `pnpm --filter @contract-guard/schema gen`，验证三个生成脚本（`gen-ts`/`gen-zod`/`gen-py`）可正常产出——这是 Phase 0 的最小验证。
2. 针对 D1–D3 给出结论；确认后我按标准 SOP 启动 Phase 0（架构师先出 `app/db` 模型与任务分解）。
3. 如需，我可先单独把 `openapi.yaml` 的端点清单整理成"接口-页面-路由"映射表，作为 Phase 1/3 的工作量估算基线。
