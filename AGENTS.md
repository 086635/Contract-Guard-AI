# AGENTS.md · 企业合同智能审核与风险预警系统

> **读者**：在本仓库工作的 AI 编码代理（以及新加入的人）。
> **本文件的定位**：**只指路 + 列不可协商的铁律 + 给可执行命令。不定义契约、不复述规范。**
> **任何定义冲突**，一律以 `文件集合/codex/0.施工标准与定义归属.md`（L0）为准。

---

## 0. 三十秒上手（每个新会话的第一步）

本仓库已进入**「文档先行 + 单任务推进」**阶段：施工文档已写到**子任务粒度**，你不需要（也不允许）自由发挥。

```text
① 领 1 个任务 ID（T-A* / T-B* / T-C*） → ② 读该任务所属的施工文档
→ ③ 只改文档写明的落点 → ④ 跑 pytest（不得低于 140 passed）
→ ⑤ 回填文档里的「实测后回填」表 → ⑥ 汇报：文件清单 + 断言逐条结果 + 回填位置
```

- 🔴 **没有任务 ID 就不要动代码。** 一次会话**只推进一个**任务 ID。
- 🔴 落点、命名、断言**一律以文档为准**；文档没说的地方不要在代码里自行发明。
- ⚠️ 本机 `pytest` 裸命令**跑不通**，必须按 **§4.1** 先设 `PYTHONPATH`。
- 🧭 **先选模式**（Goal / Default / Plan，见 **§3.1**）；命中 **§3.3 停手条件**就停下来报告，不要绕。

---

## 1. 开工前必读

> ⚠️ **照错文档干活是本仓库最大的风险。** 曾有 3 份文档各自宣称自己是「唯一依据」，其中指向废弃目录的那份恰恰自称「可直接指导编写」。先读本节再动手。

### 1.1 文档权威层级（冲突时以层级高者为准）

| 层 | 文档 | 只在这些问题上**有权威** | 不得用于 |
|---|---|---|---|
| **L0 施工** | `文件集合/codex/*` | 任务拆分、执行顺序、验收断言、**落点** | 定义契约字段 |
| **L1 契约** | `文件集合/report/RAG_MODULE_DESIGN.md` | 契约字段、接口签名、枚举取值 | 定实现细节、定命名 |
| **L2 实现** | `文件集合/report/RAG搭建框架.md` | 标识符命名、算法实现细节 | 定义契约字段 |
| **L3 事实** | `文件集合/report/项目代码深度阅读与分析报告.md` | 「仓库现在是什么样」（实测） | 设计决策 |
| **L4 计划** | `文件集合/report/codex项目推进.md` | 任务编号、决策登记 D1–D4、**开工门禁与模式编排（§12）** | 定义契约字段 |
| **L5 阅读** | `RAG白皮书.md` / `RAG对照解析.md` | 仅供**理解概念** | 任何实现依据 |
| **L6 历史** | `RAG_IMPLEMENTATION_GUIDE.md` / `AI核心开发文档.md` / `核心构建PRD.md` | 仅作**决策演进档案** | 任何实现依据 |

完整层级与三条裁决规则见 **`0.施工标准与定义归属.md` §0.11**。

### 1.2 🔴 不得作为实现依据的文档（历史档案）

- `文件集合/report/RAG_IMPLEMENTATION_GUIDE.md`
- `文件集合/report/AI核心开发文档.md`
- `文件集合/report/核心构建PRD.md`（**选型结论仍有效，目录规划已过期**）
- `文件集合/report/项目进度评估报告1.0.md`（**结论已被推翻，不要引用其中任何数字**）

> 这几份文档顶部均有「⚠️ 文档纠偏声明」，逐条列明自身过期之处。**读到了不要照做，回来找 L0~L2。**

### 1.3 🔴 三条「不存在，且不得创建」的路径

```text
packages/rag-etl/**                     ← 该包不存在
worker/storage/clause_vector_store.py   ← 已废弃
worker/storage/bm25_store.py            ← 已废弃
```

旧文档把它们标为「待新建」，照做会建出错误目录。完整落点对照见 **`0.§0.8 落点表`**、废弃清单见 **`0.§0.11.3`**。

### 1.4 旧类名 → 现行类名

| 旧名（仅存在于历史档案） | 现行名 |
|---|---|
| `ClauseChunk` | `ContractChunk`（`rag_contracts/chunk.py`，28 字段） |
| `ClauseVectorStore` | `QdrantVectorStore` |
| `ClauseAwareChunker` | `SimpleChunker`（现）/ `SectionAwareChunker`（T-A9） |

### 1.5 模块与子任务文档（**任务只从这里领**）

| 模块 | 总纲（先读它） | 子任务文档 | 状态 |
|---|---|---|---|
| **1 · RAG** | `文件集合/codex/1.RAG模块.md` | `1-1` ~ `1-8` | ✅ 已落盘，**可直接开工** |
| **2 · Agent** | `文件集合/codex/2.AGENT模块.md` | `2-1` ~ `2-9` | ✅ 已落盘，**可直接开工**（2026-09-21） |
| **3 · OCR** | `文件集合/codex/3.OCR模块.md` | `3-1` ~ `3-8` | ✅ 已落盘，**可直接开工**（2026-09-21） |
| **4 · 前端配套** | `4.前端配套模块.md` | — | ⏳ 未开始 |

**唯一权威的任务清单**是各模块总纲的 **§X.5 子任务索引**（文档名 / 任务 ID / 优先级 / 人日 / 前置依赖）；
开工前再看 **§X.6 依赖图**确认前置任务是否真的已完成。

模块 1 的三组任务（下一步就是做它）：

```text
A 组 P0 关键路径 ≈3.5 人日   T-A0(1-1) → T-A7(1-3) → T-A5/A6(1-6) → T-A8(1-7)
B 组 P2 可扩展支线 ≈3.0 人日 T-A1(1-2) → T-A2(1-3) → T-A3(1-4) → T-A4(1-5)
C 组 独立优化（可砍）        T-A9(1-8)       已取消：T-A10 数据迁移
```

⚠️ `1-3配置与工厂接线.md` **同时含 T-A7(P0) 与 T-A2(P2)**；`1-5集成测试与迁移.md` 含**已取消**的 T-A10。
**动手前先确认任务 ID，不要顺手把 P2 做掉。**

---

## 2. 仓库地图

```text
packages/
  schema/            【SSOT】openapi.yaml + 代码生成（TS / Zod / Pydantic DTO）
  rag-contracts/     契约层：Protocol 端口 + Pydantic 模型 + 导出 JSON Schema（已冻结）
  cg-observability/  日志 / 埋点 / Token 计数
  rag-core/          RAG 核心：切片 / 向量化 / 向量库适配器 / 融合 / 编排
  ai-engine/         Agent（auditor / rewriter / conflict）+ memory + prompts
  doc-processor/     解析：layout / ocr / parser / aligner
apps/
  api-gateway/       FastAPI 主 API（当前由 mock 驱动）
  async-worker/      Celery 任务（parse_doc / run_audit / gen_report）
  web-ui/            React + Vite + TS SPA
docker/              compose / Dockerfile / nginx
scripts/             建库、评测、冒烟脚本（不要放进 packages/）
文件集合/codex/       【L0】施工文档与任务分解
文件集合/report/      【L1–L4】契约、实现、事实、计划
文件集合/交付报告/     阶段性交付说明（人看的，不是实现依据）
```

---

## 3. 单任务开发循环（**照这个顺序做，不要跳步**）

> 本节是**唯一处**：`文件集合/codex/AGENTS.md` 的 §3 / §4 已改为指向本节，不再重复描述。

### 3.1 粒度与模式选择（**先选模式，再动工具**）

**粒度**：一次会话 / 一个 goal = **一个任务 ID**。

| 允许 | 禁止 |
|---|---|
| 1 个子任务文档（如 `1-1` 的 T-A0） | 一个会话里连做 `1-1` + `1-3` |
| 同一文档内的多个小任务 ID（如 `1-6` 含 T-A5 / T-A6） | 顺手改别的模块（越界） |
| 前置核销 + 实现 + 测试 + 回填 + 汇报 | 只实现、不回填 |

| 场景 | 模式 | 退出条件 |
|---|---|---|
| 执行一份子任务文档（`1-1`、`2-4`、`3-6` …） | **Goal** | §3.2 的十步全绿 |
| 补写 / 修订施工文档（同模块 1~3 篇） | **Goal**；单篇一轮能做完时用 **Default** | 9 节齐 + `文件集合/codex/AGENTS.md` §2.1 六处同步 + 该文件 §2.4 自检 |
| 前置核销、装包、单点修复、只读调查 | **Default** | 当场实测验证 |
| 需要拍板（选型、改契约层、动默认值红线） | **Plan** | 产出选项与代价 → 回填决策表 → 再进 Goal |
| 把已拍板的决策落进 L0 / L1 文档 | **Default**（改文档） | 决策表状态列改「已拍板」+ 日期 |

**Goal objective 模板**（直接复制改写）：

```text
目标：按《文件集合/codex/<文档名>.md》完成 <任务 ID>。
完成定义：① 该文档 §⑦ 断言逐条实测通过；② pytest ≥ 173 passed（命令见 §4.1）；
③ 回填该文档「实测后回填」表；④ 给出改动文件清单。
约束：只改 0.§0.8 落点表允许的文件；不新增重复定义（0.§0.7）；不越界改 service.py / rag_contracts。
停手条件：见本文件 §3.3 —— 命中即停下来报告，不要绕。
```

🔴 **一个 goal 只写一个任务 ID**；跑到一半要改目标 → 停下来重新立 goal。

### 3.2 标准作业流程（SOP 10 步）

| 步 | 动作 | 完成判据 |
|---|---|---|
| 1 | 从模块总纲 `§X.5` 领任务 ID，用 `§X.6` 核对前置 | 一句话任务声明 |
| 2 | **前置核销**：逐条实测前置产物是否存在（**不要信文档里写的「已完成」**） | 每条前置给出「命令 + 实测输出」 |
| 3 | 读该文档 9 节，抄出三件套：**落点 / 断言 / 回填表** | 落点文件清单 |
| 4 | 落点合规：对照 `0.§0.8`，避开 `0.§0.11.3` 废弃路径 | 无新增未登记文件 |
| 5 | 契约层判断：是否需要动 `rag_contracts` / `service.py`？文档未授权 → 停手 | 明确写出「不触碰」 |
| 6 | 实现：最小必要改动，风格对齐 `0.§0.2` | diff 只覆盖落点 |
| 7 | 单测：新增用例；外部依赖用 `0.§0.6.2` 的统一 skip 写法 | 用例可离线跑 |
| 8 | 门禁：全量 `pytest`（§4.1）+ 只对**改动文件**跑 ruff | ≥ 140 passed、无新增错误 |
| 9 | 跑该文档 `§⑦` 断言，逐条留证据 | 每条给出实际输出摘要 |
| 10 | **回填** `§⑧ 实测后回填` 表 + 按 §3.4 汇报 | 文档 diff + 结项说明 |

**四条禁令**：

- 🔴 不新增「同一概念的第二个定义处」——唯一处见 `0.§0.7` 定义归属表
- 🔴 不新建 `0.§0.8` 落点表以外的文件；`0.§0.11.3` 的废弃路径一律不得创建
- 🔴 不跨模块顺手改——越界清单见各模块总纲 **§X.8 与其他模块的边界**
- 🔴 不为「让测试通过」而改断言 / 放宽阈值——确有必要时**先说明理由再改**

### 3.3 停手条件（命中即停，**不要「先绕过再说」**）

| # | 情况 | 你要做的 |
|---|---|---|
| 1 | 文档写的落点 / 类名 / 字段在仓库里**不存在**或与现状矛盾 | 报告：文档哪一行 vs 实测结果 |
| 2 | 需要**网络 / API Key / 外部服务**（真实 LLM、语义服务、Milvus） | 报告：需要哪个 env；指出现行的离线替代品 |
| 3 | 需要**新增配置项却没有消费方**，或触碰默认值红线（`CG_RAG_VECTOR_STORE` 默认 `inmemory`；**Milvus 不得成为任何默认值**） | 报告：为什么必须新增 |
| 4 | 需要改 `rag_contracts` 既有字段 / 枚举，或改 `service.py`，而任务文档**未授权** | 报告：改哪个字段、影响哪些模块 |
| 5 | 前置任务的产物实测缺失 | 报告：缺什么，建议先做哪个任务 ID |
| 6 | 断言要靠**放宽阈值 / 改断言**才能过 | 报告：实测值 vs 阈值 |
| 7 | 两份文档对同一件事说法不一致 | 按 `0.§0.11.2` 裁决；**不准自己挑一个**并继续 |

**停手回报格式（3 行，不要长篇）**：

```text
卡点：<一句话>
已排除：<我试过 A / B，结果是 X / Y>
需要你定：<一个具体问题 + 我建议的默认选项>
```

### 3.4 结项汇报模板（固定字段）

```markdown
任务：T-Xx · <文档名>
改动文件：<路径 1>、<路径 2>（新增 N 个测试）
断言结果：§⑦-1 通过（<输出摘要>）；§⑦-2 通过；…
门禁：pytest <N> passed（基线 140，新增 <k>）；ruff（仅改动文件）无新增
回填位置：<文档 §⑧ 表名 / 第 N 行>
未做项：<逐条列出，或写「无」>
越界声明：无（或：我改了 X，原因是 Y，已获你确认）
```

### 3.5 跨会话 / 跨 Agent 交接

交接摘要必须含以下 **7 项**，缺一项下一个人就会重做或做错：

1. 当前 goal 的目标（原文）+ 状态（active / 完成 / 阻塞）
2. 已完成的任务 ID + **改动文件清单**
3. **实测事实**（带命令）：版本号、测试基线、关键符号是否存在
4. 关键设计决策（本会话为后续文档定下的约束）
5. 阻塞与未决问题（谁在等谁）
6. **下一步的第一件事**（精确到文件与函数）
7. 临时文件清单：本轮产生的 `_*.py` / `_*.txt` 当轮清理；**别人遗留的不要删**

> 交接摘要默认写进对话；确需落盘时进 `文件集合/report/`（L4），**不进 `文件集合/codex/`**。

---

## 4. 命令

### 4.1 测试 —— **唯一硬门禁**

```powershell
$root = "E:\important_project\Serve_bage"
$env:PYTHONPATH = "$root\.venv\Lib\site-packages;$root\packages\rag-contracts;$root\packages\rag-core;$root\packages\cg-observability;$root\packages\ai-engine;$root\packages\doc-processor;$root\apps\api-gateway;$root\apps\async-worker"
$env:TEMP = "$root\.tmp_t"; $env:TMP = "$root\.tmp_t"      # 避开受限的 %TEMP%
New-Item -ItemType Directory -Force "$root\.tmp_t" | Out-Null
D:\aanacoda1\python.exe -m pytest -q --basetemp="$root\.tmp_t\bt" -p no:cacheprovider
```

单包 / 单文件：把 `-q` 之后换成路径，例如 `D:\aanacoda1\python.exe -m pytest packages/rag-core -q`。

- ⚠️ **不要裸敲 `pytest`**：本机默认 `pytest` 走**系统 Python**（`D:\aanacoda1`），实测会因 `pydantic-core 2.27.2` 与 `pydantic` 不匹配直接 `SystemError`。**必须**先设上面的 `PYTHONPATH`，让 `.venv` 的包优先。
- **离线基线：173 passed / 1 skipped**（2026-09-21 T-A0 / T-C0 / T-A7 / T-C1 之后；T-A0 前为 140）。
  少于 173 = 你删坏了东西；多于 173 = 你加了测试（好事，但要在汇报里说明）。
  skip 是**设计内**的：`CG_OCR_TEST_PADDLE` 未设置时跳过 PaddleOCR 路线断言（`0.§0.6.2`）。
- ℹ️ 仓库根已有 `pytest.ini`（T-A0-4）：`pythonpath` 含各 workspace 包与 `.venv/Lib/site-packages`，**手工 `PYTHONPATH` 不再必需**；但本机 `%TEMP%` 受限，仍建议按上面的命令设 `TEMP/TMP`。
- 依赖外部服务（Milvus / HTTP 模型 / DB）的测试，**未配置环境变量时必须 skip，不得 fail、不得 error**——统一写法见 `0.§0.6.2`。

### 4.2 静态检查 —— ⚠️ **当前不为零，不要声称「全绿」**

| 命令 | 现状（2026-09-21 实测） |
|---|---|
| `.\.venv\Scripts\ruff.exe check .`（ruff **0.16.4**） | **53 项**：`I001` 10 / `UP035` 9 / `UP042` 9 / `F841` 8 / `RUF022` 7 / `F401` 6 / `UP017` 3 / `RUF100` 1 |
| `.\.venv\Scripts\ruff.exe format --check .` | **42 个文件**待格式化 |

⚠️ **同一份代码换个 ruff 版本数字就不同**（系统的 `D:\aanacoda1\Scripts\ruff.exe` 是 0.12.0，只报 **44 项**）。
**引用这些数字时必须连版本一起写**，不要跨版本比较「变好了还是变坏了」。

全仓清理**不在**当前任务范围内。要求只有一条：**你改动的文件不要新增 ruff 错误。**

### 4.3 其它

```powershell
pnpm --filter @contract-guard/schema gen            # SSOT 代码生成（改 openapi.yaml 后必跑）
pnpm --filter @contract-guard/web-ui dev            # 前端开发
pnpm --filter @contract-guard/web-ui build          # tsc -b && vite build
pnpm --filter @contract-guard/web-ui lint           # tsc --noEmit
docker compose -f docker/docker-compose.yml up -d   # 基础设施（postgres/redis/qdrant/minio/api/worker/web/nginx）
python scripts/phase1_smoke.py                      # RAG 全链路冒烟（离线）
```

---

## 5. 硬红线

### 5.1 依赖方向（**反向依赖 = 返工**）

```text
rag-contracts ──> rag-core ──> ai-engine ──> async-worker
                     ↑
               api-gateway
```

- 🔴 `rag-core` **不得** import `ai_engine` / `worker` / `apps/*`
- 🔴 `rag-core/rag_core/service.py` **只依赖 `rag_contracts.ports` 的 Protocol**，不得 import 任何具体 SDK（`qdrant_client` / `pymilvus` / `httpx`）
- 🔴 `rag-core` **禁止**引入 `torch` / `sentence-transformers` / `jieba` / `rank_bm25`；需要精排 → **走 HTTP**

### 5.2 端口-适配器（**换实现 = 加文件 + 加分支**）

所有「会变的实现」（向量库 / 稀疏检索 / 向量化 / 重排）必须实现 `rag_contracts.ports` 的 Protocol，经 `rag_core.factory` 装配。
**绝不动 `service.py` 与 `rag_contracts`。**

### 5.3 其它不可协商项

- 🔴 **生成物禁止手改**：`apps/api-gateway/app/generated/schemas.py`、`apps/web-ui/src/api/generated/{types.ts,zod.ts}`
- 🔴 **同一概念只允许唯一定义处**（见 `0.§0.7` 定义归属表）；其它文件只 import，禁止复制
- 🔴 **默认路径红线**：`CG_RAG_VECTOR_STORE` 的代码默认值是 `inmemory`；生产/演示在 `.env` 显式设 `qdrant`。**Milvus 不得成为任何默认值**
- 🔴 **条款编号正则不得另写一份**：唯一处是 `rag_core/chunking/simple_chunker.py` 的 `_CLAUSE_HEAD_RE`（否则会出现「解析层认为的第 3 条 ≠ 检索层认为的第 3 条」）
- 🔴 **禁止在 `ai_engine/rag/*` 重新实现检索**：一律包装 `rag_core` 的 `retrieve_contract_knowledge`（F6 的成因）

---

## 6. 完成定义（DoD）

每个任务必须逐条满足：

1. 施工文档的「验收标准 / 可执行断言」**逐条通过**
2. `pytest` **全绿，且不少于基线 173 passed**（允许设计内的 skip）
3. **不新增** ruff 错误
4. **回填文档**：施工文档里的「实测后回填」表格（如 `1-6` §⑧ 阈值、`1-7` §⑧ 基线登记表）必须回填；**弱标注集与人工标注集分别记录、不得混淆**
5. 给出改动的文件清单

> ⚠️ **不回填 = 文档会重新落后于代码**，这正是本项目上一轮花大力气消除的问题。**回填是 DoD 的一部分，不是可选项。**

---

## 7. 当前事实基线（2026-09-21 实测）

| 事实 | 值 |
|---|---|
| 测试基线 | **173 passed / 1 skipped**（2026-09-21，约 6 秒，全离线；1 skip = PaddleOCR 路线断言） |
| 契约字段 | `ContractChunk` 28 字段；`RagSettings` **35** 字段（T-A7 删除重复声明 `log_level`，原 36） |
| 端口（Protocol） | 5 个：`VectorStorePort` / `SparseStorePort` / `EmbedderPort` / `RerankerPort` / `TokenCounterPort` |
| `rag_core/adapters/` | 8 个文件（含 `qdrant_store` / `inmemory_store` / `bm25_store`） |
| 向量库 | **Qdrant 主力**（`qdrant_client 1.19.0` 已装）；Milvus 为 **P2 备选**（`pymilvus` **未装**） |
| `.venv` 已装 | `langgraph 1.2.11` / `langchain_core 1.6.0` / `openai 3.3.1` / `paddleocr` / `pdfplumber` / `pypdfium2` / `opencv` |
| `.venv` 缺包 | `pymilvus`、`rank_bm25`、`jieba`、`sentence_transformers`、`langchain-openai`、`langgraph-checkpoint-postgres/redis`（**`celery` 已于 T-A0 收口进 `.venv`**；`paddleocr` / `paddlepaddle` 已于 T-C0 移除，OCR 走 RapidOCR） |
| 依赖判据（**2026-09-21 实测补充**） | 上两行的「已装 / 缺包」一律以 **`.venv\Lib\site-packages`** 为准；系统 Python（`D:\aanacoda1`）里的包在 §4.1 前缀下会**顺带可见**，**不代表已收口**。实测（T-A0 / T-C0 之后）：`pymilvus` / `jieba` / `rank_bm25` / `sentence_transformers` / `langchain_openai` / `langgraph-checkpoint-postgres` / `torch` **仍在系统 Python、不在 `.venv`**；`celery` / `redis` / `qdrant_client` / `rapidocr_onnxruntime` / `onnxruntime` / `pymupdf` / `cv2` / `numpy` / `pypdfium2` **已在 `.venv`**；`redis` / `langgraph` / `langchain_core` / `openai` / `paddleocr` / `pdfplumber` / `python-docx` / `pypdfium2` / `opencv(headless)` / `numpy` / `qdrant_client` / `minio` 在 `.venv` 内 |
| 语义层 | 🟡 默认 `HashEmbedder` + `NoopReranker`（**伪向量 / 无精排**，是召回率瓶颈） |
| 产品接入 | 🟡 `rag_core` 目前**只被 `scripts/phase1_smoke.py` 消费**；`api-gateway` 仍是 mock 驱动 |
| 文档 vs 代码 | 模块 1 / 2 / 3 的施工文档**已全部落盘**（`1-1`~`1-8` / `2-1`~`2-9` / `3-1`~`3-8`）；但 Agent（13 py / 486 行）、OCR（5 py / 175 行）、`async-worker` 仍是骨架，共 **11 处 `NotImplementedError`**、**零测试** |

> 要引用「现状」，一律以 **L3 `项目代码深度阅读与分析报告.md`** 为准；不要引用历史档案里的数字。

---

## 8. 本机已知坑（Windows + PowerShell）

| 坑 | 表现 | 绕过 |
|---|---|---|
| `.venv\Scripts\python.exe` 无法启动 | venv 重定向器受限 | 用系统 Python `D:\aanacoda1\python.exe`，把 **`.venv\Lib\site-packages` 放 `PYTHONPATH` 首位** |
| 裸敲 `pytest` | 系统 Python 的 `pydantic-core 2.27.2` ≠ 当前 `pydantic` → `SystemError` | 走 §4.1 的 `python -m pytest` + `PYTHONPATH` |
| `PYTHONPATH` 分隔符 | 用 `:` 会 `ModuleNotFoundError` | Windows 用 **`;`** |
| `%TEMP%` 受限 | pytest 建临时目录失败 | `TEMP`/`TMP` 指向 `$root\.tmp_t`；`--basetemp` 的**父目录要先存在** |
| PowerShell 5.1 读含中文的文件 | GBK 解码 → 乱码 | 用 Python `io.open(..., encoding="utf-8")` 读写中文文件 |
| 控制台输出中文 / emoji | `UnicodeEncodeError: 'gbk' codec`（**只是显示问题，文件内容多半是好的**） | 先写入 UTF-8 文件再读；或只打印码点十六进制做校验 |
| 文件被截断为 0 字节 | 先 `open("wb")` 后 `encode()` 失败所致 | **先 `data = txt.encode("utf-8")`，再 `open(..., "wb")`** |
| 写含中文的文件 | 手敲命令易被编码破坏 | PowerShell 单引号 here-string + `[IO.File]::WriteAllText($p,$t,(New-Object System.Text.UTF8Encoding($false)))`（已验证：中文/emoji 0 丢失） |
| `.venv\Scripts\ruff.exe` 可正常运行 | 只有 `python.exe` 不可用 | ruff / 其它工具 exe 直接用 `.venv\Scripts\*.exe`；⚠️ `pytest.exe` / `mypy.exe` 也是 `python.exe` 启动器，**同样不可用** → 一律 `python -m pytest` / `python -m mypy` |
| 在 uv workspace 里 `uv add/remove --package X` | 只同步该成员，**会把其它成员的依赖从 `.venv` 剪掉**（实测：celery / redis / qdrant-client 被剪，测试凭空多出 2 个 skip） | 紧随一次 **`uv sync --all-packages`**；判断信号：pytest 出现本不该有的 skip |
| 裸敲 `pytest`（无 `TEMP`） | `pytest.ini` 已兜住包路径，但 `tmp_path` 仍撞本机 `%TEMP%`（`C:\DevTemp`）权限 | 仍需 `TEMP`/`TMP` → `$root\.tmp_t`（§4.1）；或 `--basetemp` |

---

*维护约定*：**本文件只增不删**；同类错误出现第三次时才新增规则。
*最后更新：2026-09-21（Gate 0 收尾 + W1 起步：T-A7 配置接线、T-C1 文档分流器落盘，基线 173 passed、`RagSettings` 35 字段）*