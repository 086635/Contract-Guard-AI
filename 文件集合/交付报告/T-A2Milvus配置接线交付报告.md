# T-A2 · Milvus 配置接线 交付报告（排期项 `0-C1`）

> 交付日期：**2026-09-23**　范围：`文件集合/codex/0-C1.md`（权威规格：`文件集合/codex/1-3配置与工厂接线.md` §⑤~§⑦ / §⑨~§⑪）
> 分支：`codex/impl-rag-agent-ocr`　提交：`11ec9fc`（实现）、`80d4593`（守卫 + 读数回填）
> 本报告是**人看的交付说明**，不是实现依据 —— 实现依据一律以 `文件集合/codex/**`（L0）为准。
> 反过来，本报告里的**实测数字与偏差登记**应当反过来修正文档（本轮已改的都在 §4 / §5 列出）。

---

## 0. 一页速览

| 交付项 | 状态 | 关键证据 |
|---|---|---|
| **配置层**：`RagSettings` +5 个 `milvus_*` 字段 | ✅ | `python -c "from rag_core.config import RagSettings; s=RagSettings(); print(s.milvus_mode, s.milvus_uri, s.milvus_timeout_s)"` → `server http://localhost:19530 5.0`；字段数 **35 → 40** |
| **装配层**：`vector_store="milvus"` 分支 | ✅ | `vector_store` 的 `Literal` = `['inmemory','milvus','qdrant']`、默认仍 `inmemory`；分支**函数内延迟导入**，复用 `settings.collection`，`milvus_timeout_s` 真实透传 |
| **示例配置**：`.env.example` 的 `CG_RAG_MILVUS_*` 段 | ✅ | 整段**注释**（默认离线路径不受影响）；向量库分组头补 `milvus` 一行 |
| **测试**：`test_config_factory.py` +8 条 / `conftest.py` +`fake_pymilvus` 夹具 | ✅ | `pytest packages/rag-core` → **165 passed / 6 skipped** |
| **文档回填与纠偏** | ✅ | `0-C1.md`（回填 3 行 + 新增 §⑩）、`1-3`（§⑥ 纠偏块 + 新增 §⑫）、`1.RAG模块.md`、`1-2Milvus适配器.md`、`AGENTS.md` |

**一句话**：`MilvusVectorStore`（T-A1 已实现）**现在真的能被配置选上了** —— `CG_RAG_VECTOR_STORE=milvus` 走通配置 → 工厂 → 适配器三段接线，
而**默认路径（`inmemory`）与回退路径（`qdrant`）一行未动**；同时核销了两处历史遗留的文档矛盾（`1-3 §⑥` 的"顶层急切 import"作废）。

## 1. 本轮做了什么（顺序）

1. **前置核销**：确认 T-A1 产物在位（`adapters/milvus_store.py`、PEP 562 懒导出、`pyproject` 的 extra `milvus`），并实测 `config.py` / `factory.py` / `.env.example` 的改动前状态。
2. **实现**：`config.py` 加字段 + 扩 `Literal`；`factory.build_vector_store()` 加分支；`.env.example` 加段。
3. **单测**：按 `0-C1 §⑧` 加 6 条，并补 2 条加测（`timeout_s` 透传、`factory` 顶层延迟导入守卫）→ 共 **8** 条。
4. **跑卡内 `§⑦` 8 条断言**（逐条见 §3），其中第 1 条按实测改判（见 §4）。
5. **回填**：`0-C1.md` 的「实测后回填」+ 落地记录；任务层 `1-3` 的纠偏与落记录；模块索引与事实基线同步。

## 2. 逐项交付

| 落点（`0-C1 §④`） | 改了什么 | 关键口径 |
|---|---|---|
| `packages/rag-core/rag_core/config.py` | +`milvus_mode` / `milvus_uri` / `milvus_token` / `milvus_db_name` / `milvus_timeout_s`；`vector_store: Literal["inmemory","qdrant","milvus"]` | 默认值逐字对齐 `0-C1 §⑤`；**保留** `inmemory`/`qdrant` 两个既有取值且默认仍 `inmemory`；字段插在 `qdrant_api_key` 之后（按 `1-3 §⑤` 的"紧邻既有 `qdrant_*`"） |
| `packages/rag-core/rag_core/factory.py` | `build_vector_store()` 新增 `milvus` 分支（qdrant 分支之后） | **函数内延迟导入** `MilvusVectorStore`；**复用 `settings.collection`**（不新增 collection 配置项，保证与 Qdrant 双跑可对比）；`timeout_s=settings.milvus_timeout_s` **真透传**（否则该配置是死配置） |
| `.env.example` | 新增 `CG_RAG_MILVUS_*` 段（5 项，**全注释**） | 默认路径保持离线；注释里写明 Windows 只能用 `server` 模式、服务未起时会在构造客户端时抛 `StoreUnavailableError` |
| `packages/rag-core/tests/test_config_factory.py` | +8 条 | 三分支类型 / 非法取值 / Literal 取值 / 5 个默认值 / `collection` 复用 / 连接参数透传 / 无服务时异常类型 / 顶层延迟导入守卫 |
| `packages/rag-core/tests/conftest.py` | +`fake_pymilvus` 夹具 | 让"milvus 分支返回正确类型"这条接线断言**离线可跑**（注入假 SDK，不联网） |

## 3. 实测证据

### 3.1 卡内 `§⑦` 八条断言

| # | 断言 | 结果 |
|---|---|---|
| 1 | `vector_store=milvus` → `MilvusVectorStore` | ⚠️ **原文"不建连"不成立**（该适配器**构造即建连**）→ 改判为两条替身，均✅（见 §4-1） |
| 2 | `inmemory` → `InMemoryVectorStore`（默认路径不破） | ✅ |
| 3 | `qdrant` → `QdrantVectorStore`（回退路径不破） | ✅ |
| 4 | 非法取值抛 `ValueError` 且含取值 | ✅ `"bogus" in str(exc)` |
| 5 | `Literal` 含 3 值、默认 `inmemory` | ✅ `['inmemory','milvus','qdrant']` / `default: inmemory` |
| 6 | 包出口可导入 + 缺 `pymilvus` 时 `import rag_core` 仍成功 | ✅ **核销** T-A1 用例（`-k "package_export or no_top_level"` → **2 passed**） |
| 7 | 5 个 `milvus_*` 默认值正确 | ✅ `server / http://localhost:19530 / '' / default / 5.0` |
| 8 | 离线基线不退化 | ✅ 见 §3.2 |

### 3.2 门禁（唯一硬门禁）

| 项 | 读数 |
|---|---|
| 全量 `pytest` | **基线 398 passed / 8 skipped / 0 failed**。收集数 **406**（= T-A2 前 398 收集 + 本卡新增 8）；8 条 skip 全为设计内（4 Milvus 集成 / 1 真实 embedding / 1 真实精排 / 1 PaddleOCR 路线 / 1 worker 接缝） |
| `rag-core` 单包 | **165 passed / 6 skipped**（157 + 8 条新增） |
| `ruff check` / `ruff format --check`（仅改动文件） | 零新增 |
| `mypy packages/rag-core/rag_core` | 19 文件无问题 |
| `python scripts/phase1_smoke.py`（默认离线） | **exit 0**，`store=inmemory`（默认未被污染） |

> ⚠️ **读数的前提**：本机当时被桌面应用（WPS / 微信 / 钉钉 / Excel）占满，三次全量跑了 **304 s / 588 s / 814 s**（空闲约 73 s），
> 出现 **1~2 条"负载假红"**（都在 `doc-processor`，与本次改动无因果关系）：
> ① 真实 OCR 用例 `test_ocr_page_returns_boxed_lines`：负载下单条 74~94 s > 生产默认 `timeout_s=60`（负载较轻时单独复跑 **8.5 s 通过**）；
> ② `test_aligner.py::test_locate_is_fast` 的**计时守卫**（`elapsed < 0.5 s`）负载下实测 **0.622 s**（单独复跑通过）。
> 这两个成员已写进 `AGENTS.md §4.1` 的"负载假红家族"，作为**读数前提**。

### 3.3 怎么用（启用 Milvus）

```powershell
# 1) 起 Milvus 服务：见 docker/docker-compose.yml（T-A3 / 0-C2，尚未落地）
# 2) 在 .env 里显式切换（代码默认永远是 inmemory）
CG_RAG_VECTOR_STORE=milvus
CG_RAG_MILVUS_MODE=server
CG_RAG_MILVUS_URI=http://localhost:19530
CG_RAG_MILVUS_TIMEOUT_S=5.0
# 3) 验证接线（不建连的类型断言用假 SDK；真路径断言见用例命名）
D:\aanacoda1\python.exe -m pytest packages/rag-core/tests/test_config_factory.py -k milvus
```

## 4. 偏差与纠偏登记（本轮改了哪些文档、为什么）

1. **`0-C1 §⑦-1` 的"不建连"写法不可执行 → 已改判（卡片内 §⑩.1 有完整记录）**
   `MilvusVectorStore.__init__` **构造即建连**（`MilvusClient(...)` 直接握手，`1-2 §⑪.3-#3` 已实测），无服务时必然抛 `StoreUnavailableError`。
   改为两条互为补充的离线断言：**假 `pymilvus` 验类型** + **真路径验异常类型**（后者才证明"分支真的接上了"；设 `CG_RAG_TEST_MILVUS_URI` 时自动 skip）。
   代价：该用例 ≈ 8.9 s（含 `pymilvus` 真实 import 与一次连接失败），是本卡最慢的一条，已知且可接受。
2. **`1-3 §⑥` 的"`adapters/__init__.py` 顶层急切 import 是安全的" → 已加纠偏块作废（原文保留）**
   T-A1 实际采用 **PEP 562 懒导出**，并**禁止**顶层急切导入；断言 6 在 PEP 562 下同样成立
   （`from m import X` 会走模块级 `__getattr__`），且缺 `pymilvus` 时 `import rag_core` 仍成功。
   **故 T-A2 未改 `adapters/__init__.py`**（`0-C1 §⑥-4` 的裁决），并留下核销证据（T-A1 用例 2 passed）。
3. **字段落位取 `1-3 §⑤` 的"紧邻既有 `qdrant_*`"**（卡片 §⑤ 只说"向量库分组末尾"；两种读法都在同一分组内，取更具体者）。

## 5. 事实基线更新（截至 2026-09-23）

| 事实 | 变化 |
|---|---|
| `RagSettings` 字段数 | **35 → 40**（+5 个 `milvus_*`） |
| `CG_RAG_VECTOR_STORE` 取值 | `inmemory` / `qdrant` / **`milvus`**（**默认仍是 `inmemory`**，Milvus 未成为任何默认值） |
| 测试基线 | **390 → 398 passed / 8 skipped** |
| 模块 1 剩余排期项 | **T-A3（`0-C2` 容器编排）/ T-A4（`0-C3` 集成测试）/ `0-C4`（阈值扫参）** |
| 已同步文档 | `0-C1.md`、`1-3`（§⑥ 纠偏 + §⑫ 落地记录）、`1.RAG模块.md`（组件索引 C9：40 字段 + 分支已接线）、`1-2Milvus适配器.md`（§⑪ 核销行）、`AGENTS.md`（§1.5 / §3.2 / §4.1 / §6 / §7 / 最后更新） |

## 6. 未收口项与风险

| # | 事项 | 说明 |
|---|---|---|
| 1 | **Milvus 未接入默认路径** | 默认仍是 `inmemory`（离线红线）；要跑真库需显式改 `.env`，且**服务需先起**（T-A3 / `0-C2` 未做） |
| 2 | **4 条 Milvus 集成用例仍 skip** | `pymilvus` 只在**系统 Python**、不在 `.venv`（extra 声明），且无 `CG_RAG_TEST_MILVUS_URI` → 设计行为 |
| 3 | **负载假红家族** | 真实 OCR 用例 + aligner 计时守卫在桌面应用占满 CPU 时会假红（§3.2 的读数前提） |
| 4 | **未做** | T-A3 容器编排、T-A4 集成测试 7 条断言、`0-C4` 阈值扫参（均为后续排期项，不阻塞达标） |

## 7. 下一步

1. **`0-C2`（T-A3）Milvus 容器编排**：`docker/docker-compose.yml` 加 etcd + minio + milvus 可选 profile → 起服务后把 `CG_RAG_TEST_MILVUS_URI` 指向它。
2. **`0-C3`（T-A4）集成测试**：按 `1-5 §⑤` 的 7 条断言扩展 `test_milvus_store.py`（现有 4 条 + 补 payload 往返 / 过滤含 `"` / delete 语义等）。
3. **`0-C4` 阈值扫参**：用已就绪的评测集（人工集 Recall@5 = **0.933**）扫 `score_threshold` / `rerank_top_n`，回填 `1-6 §⑧`。

## 8. 交付物与提交记录

| 提交 | 内容 | 规模 |
|---|---|---|
| `11ec9fc` | `feat(T-A2)`：`config.py` / `factory.py` / `.env.example` / `test_config_factory.py` / `conftest.py` / `0-C1.md`（回填）/ `1-3` / `1.RAG模块.md` / `1-2Milvus适配器.md` / `AGENTS.md` | 10 files, +512 / −36 |
| `80d4593` | `test(T-A2)`：`factory` 顶层延迟导入守卫 + 基线读数回填（398/8；含负载假红家族说明） | 4 files, +63 / −25 |

## 附录 A · 可复制验收命令

```powershell
$root = "E:\important_project\Serve_bage"
$env:PYTHONPATH = "$root\.venv\Lib\site-packages;$root\packages\rag-contracts;$root\packages\rag-core;$root\packages\cg-observability;$root\packages\ai-engine;$root\packages\doc-processor;$root\apps\api-gateway;$root\apps\async-worker"
$env:TEMP = "$root\.tmp_t"; $env:TMP = "$root\.tmp_t"

# 卡内用例（含 milvus 接线 8 条）
D:\aanacoda1\python.exe -m pytest packages/rag-core/tests/test_config_factory.py -v

# 三分支类型（第 1/2/3 条断言的等价写法）
D:\aanacoda1\python.exe -c "from rag_core.config import RagSettings; from rag_core.factory import build_vector_store; from rag_core.adapters import InMemoryVectorStore, QdrantVectorStore; s=RagSettings(_env_file=None); print(type(build_vector_store(s)).__name__); print(type(build_vector_store(s.model_copy(update={'vector_store':'qdrant'}))).__name__)"

# 全量门禁（注意：--basetemp 每次换新目录；不要再加 -q，pytest.ini 已有）
$bt = "$root\.tmp_t\bt$(Get-Date -Format yyyyMMddHHmmss)"
D:\aanacoda1\python.exe -m pytest --basetemp="$bt" -p no:cacheprovider
```

## 附录 B · 与 `0-C1.md` 的章节对应

| `0-C1.md` | 本报告 |
|---|---|
| §② 范围（做/不做） | §2 逐项交付 |
| §⑤ 接口规格 | §2（字段与分支口径） |
| §⑥ 实现规范（含 §⑥-4 裁决） | §4-2 / §4-3 |
| §⑦ 验收标准 + 实测后回填 | §3.1 / §3.2 / §4-1 |
| §⑧ 测试要求 | §2（+8 条）与 §3.1 第 6 条的核销证据 |
| §⑨ 避坑清单 | §2 / §4（六条坑逐条对照落地） |
