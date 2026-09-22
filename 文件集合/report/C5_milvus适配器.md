# 01 · T-A1 Milvus 适配器 · 任务概要（组件卡 C5 收口）

> **文档性质**：交付 / 交接说明（**L4 报告层**）。**不是实现依据** —— 实现依据见
> `文件集合/codex/1-2Milvus适配器.md`（任务层）与 `文件集合/codex/1.C5Milvus向量库.md`（组件卡）。
> 按 `文件集合/codex/AGENTS.md` §3.5，交接摘要落盘进 `文件集合/report/`，**不进** `文件集合/codex/`。

| 项 | 值 |
|---|---|
| 任务 ID | **T-A1**（P2 · B 组可扩展性支线，不阻塞达标） |
| 任务文档 / 组件卡 | `1-2Milvus适配器.md` / `1.C5Milvus向量库.md`（组件 C5-Δ） |
| 完成日期 | **2026-09-22** |
| 状态 | ✅ **完成**（断言全绿、回填完毕） |
| 决策依据 | **D1**：Qdrant 主力 + Milvus 备选；**Milvus 不得成为任何默认值** |

---

## 0. 一句话结论

Milvus 备选适配器 `MilvusVectorStore` 已按组件卡落点**新建并实测收口**：实现 `VectorStorePort` 全部方法、
`pymilvus` 全程延迟导入、默认路径（`inmemory`）零影响；**"换库 = 加文件 + 加分支、业务层零改动"已被真机证据证明**
（pymilvus 3.0.0 + milvus-lite 3.0），同时**实测推翻了 4 处文档预期**并逐条处置与回填。

## 1. 交付物

| 文件 | 动作 | 职责 / 关键点 |
|---|---|---|
| `packages/rag-core/rag_core/adapters/milvus_store.py` | **新建**（422 行） | `MilvusVectorStore`：`ensure_collection` / `upsert` / `search` / `delete_by_doc` / `health` / `count`；payload 双层存储（JSON 保真 + 6 个冗余标量列）；写读共用主键常量 `_PK_FIELD` |
| `packages/rag-core/tests/test_milvus_store.py` | **新建**（521 行，**21 条用例**） | 离线 17 条（协议自证 / 延迟导入 / 默认值红线 / `_escape`·`_build_expr`·`_row_of`·分数口径 / 列宽 / `ensure_collection` 两条路径）+ 集成 4 条（`skipif`） |
| `packages/rag-core/rag_core/adapters/__init__.py` | 修改 | **PEP 562 懒导出**三处（`TYPE_CHECKING` / `__all__` 按字母序 / `__getattr__`），缺 `pymilvus` 时包出口照常可用 |
| `packages/rag-core/pyproject.toml` | 修改 | extra `milvus = ["pymilvus>=2.4,<4.0"]`，注明"只走 `CG_RAG_VECTOR_STORE=milvus` 显式启用、严禁成为默认值" |
| `uv.lock` | 修改 | 随 pyproject 同步（`uv lock --check` 通过；只锁不装，未触碰 `.venv`） |

**未触碰**：`service.py`、`rag_contracts/**`、`config.py`、`factory.py`（属 T-A2）、`QdrantVectorStore`、compose。

## 2. 实测关键发现（**本次最有价值的部分**）

> 测试环境：`pymilvus 3.0.0` + `milvus-lite 3.0`（装在**系统 Python**，不在 `.venv`；本机 Docker daemon 未起）。
> 以下 4 处**与任务文档预期相反或未覆盖**，均已落到代码 + 守卫用例。

| # | 文档原预期 | 实测结果 | 处置 |
|---|---|---|---|
| 1 | `1-2 §⑤`：`COSINE` 时 `distance` 即相似度（越大越相似） | 🔴 **是余弦距离 `1 - cos`**：同向量自检索 `0.0`、正交 `1.0`、反向 `2.0`（对照实验：`L2` 返真实距离、`IP` 返点积） | 新增 `_score_from_distance()` → `score = 1 - distance`，与 Qdrant / InMemory **同口径**；守卫用例 `test_score_from_distance_is_similarity`。**不修则全库排序反向** |
| 2 | `1-2 §⑦-5`：`add_index(index_type="INVERTED")` 视版本决定 | 🔴 **不支持**：索引翻译层强制每个索引给 `metric_type`，引擎侧又只接受向量字段 → 带标量索引的 `create_collection` **整体失败** | 按 §⑦-5「不支持则去掉该循环」→ **不建标量索引**（expr 过滤结果仍正确，仅失去过滤加速）；守卫用例锁死"只建向量索引" |
| 3 | `1-2 §⑥` 骨架：`delete()` 返回 dict，取 `delete_count` | ⚠️ 实测返回 **`list[str]`**（服务端回主键时）；不回主键时才是 `OmitZeroDict({"delete_count":…, "cost":…})`（**零值会被省略**） | `delete_by_doc()` 三分支兼容（dict / list-tuple / 带属性对象） |
| 4 | `1-2 §⑥` 骨架：命中取 `row["id"]` | ⚠️ pymilvus 3.0 命中字典的键是**主键字段名** `chunk_id`（`search_result.py:151` 用 `pk_name`） | 新增唯一处常量 `_PK_FIELD = "chunk_id"`，schema 写入与命中读取共用 |

另有两处**文档未覆盖但必须处理**的实测点：

- **集合 load 时机**：跨进程 / 重启后集合处于 `released`，`search` 报 `code=101` → `ensure_collection()` 的"已存在"分支补 `load_collection()`（`load` 幂等，实测连调 2 次均 OK）。
- **构造即建连**：`MilvusClient(uri=…)` 在服务不可达时**构造就抛异常** → 统一包装为 `StoreUnavailableError`（与既有适配器一致，`retrieve()` 才能走降级）。

## 3. 验证证据

**卡内 `1.C5` §⑦ 逐条**

| # | 结果 |
|---|---|
| 1 未装 pymilvus 也不影响默认路径 | ✅ `sys.modules['pymilvus']=None` 后 `build_service_from_settings()` → `offline ok -> RAGService` |
| 2 协议自证 | ⚠️ **原文写法不可离线自证**（`M()` 会真建连）→ 已改判并给出等价离线自证（注入假 `pymilvus`：`isinstance(store, VectorStorePort)`；缺包时包出口仍可导入） |
| 3 默认值红线未破 | ✅ `RagSettings().vector_store == "inmemory"`，且 `build_vector_store(settings)` 仍是 `InMemoryVectorStore` |
| 4 用例集合 | ✅ 未设 URI：**17 passed / 4 skipped**（离线用例照跑）；设 URI：**21 passed / 0 skipped** |
| 5 离线基线 | ✅ 干净时 **334 passed / 6 skipped / 0 failed**（exit 0，出现 2 次） |

**任务 `1-2` §⑧ 8 条**：① 缺包可 import ✅ ② 包出口可导入 ✅ ③ 满足 `VectorStorePort` ✅
④ `name == "milvus:server"` ✅ ⑤ 非法 mode 抛 `StoreUnavailableError` ✅ ⑥ payload 可 `ContractChunk.from_payload()` 还原 ✅
⑦ 含 `"` 的值不产生 expr 语法错 ✅ ⑧ 离线基线未退化 ✅

**门禁数字（唯一硬门禁）**

| 项 | 结果 |
|---|---|
| `pytest`（全量，命令见 `AGENTS.md §4.1`） | 收集 **340** = 基线 **319**（`317 passed + 2 skipped`）+ 本次新增 **21**；干净时 **334 passed / 6 skipped / 0 failed**；**现有用例零退化** |
| `rag-core` 单包 | **101 passed / 4 skipped / 0 failed**（每次全绿） |
| `ruff check`（改动文件） | **0 新增**（3 个 python 文件全过） |
| `ruff format --check`（改动文件） | 3 个文件均"已格式化" |
| `mypy packages/rag-core/rag_core` | 17 文件 **无问题**（pymilvus 无 `py.typed`，import 处已 `# type: ignore[import-untyped]`） |
| `uv lock --check` | ✅ 通过 |

## 4. 回填与纠偏位置（DoD 第 4 条）

| 文档 | 回填内容 |
|---|---|
| `文件集合/codex/1.C5Milvus向量库.md` | §⑤ 接口纠偏（`uri` 默认值 / `timeout_s` 必须透传）；§⑥「实测后回填」4 行 + 追加 **6 行版本差异表**；§⑦ 追加「§⑦ 逐条实测结果」+ 第 2 条纠偏说明；§⑧ 追加「落地补充」（实际用例名 ↔ 断言对应） |
| `文件集合/codex/1-2Milvus适配器.md` | 新增 **§⑪ 落地记录与实测纠偏**（按 `0.§0.11.2` 规则 3：追加、不删原文）：⑪.1 交付物差异 / ⑪.2 §⑦ 6 点逐条结果 / ⑪.3 §⑤§⑥ 的 6 处必要偏离 / ⑪.4 §⑧ 落地情况 / ⑪.5 集成实跑方式 |
| `文件集合/codex/1.RAG模块.md` | §1.5.1 组件索引 C5 行：🟡 增量卡 → ✅ **已实现** + 证据（测试文件） |

## 5. 未做项与边界声明

- **未做**：真机 `server` 模式联调（需 T-A2 配置接线 + T-A3 容器编排；本机 Docker daemon 未起，故集成跑用 `milvus-lite` 本地 `.db` 后端）。
- **未做**：`scripts/milvus_smoke.py`（`1-2 §⑦` 的"💡建议"、非落点；其验证价值已由 4 条集成用例固化，且更可重复）。
- **未做**：标量 `INVERTED` 索引（目标版本不支持，按 §⑦-5 放弃，仅损失过滤加速）。
- **边界**：只改组件卡列出的 4 个落点（+ `uv.lock` 同步）；未触碰契约层 / `service.py` / 默认值 / `factory`。
- **越界声明**：无。附带裁决了一处文档冲突 —— `1-3 §⑥` 写"`__init__.py` 顶层急切 import 是安全的"，
  与 `1-2 §④`（+ §⑩ 坑 11）禁止顶层急切导入冲突；采用 **PEP 562 懒导出**（同时满足 `1-3 §⑨` 断言 6），
  已在 `1-2 §⑪.1` 注明请 **T-A2 落地时修正该片段**。

## 6. 已知遗留（**非本任务，未处置**）

`packages/doc-processor/tests/test_ocr_engine.py::test_ocr_pdf_page_uses_render_dpi` 是**环境敏感**用例：
它走真实 RapidOCR 且页级超时固定 **30 s**，而实测单页 A4@200dpi 在本机需 **47.3 s**，机器一忙即超时。
**改动前的基线首跑就是这条红**；`doc_processor` 生产代码不 import `rag_core`（依赖方向相反），与 T-A1 无因果关系。
属模块 3 / T-C2 边界，建议后续单独处置（例如把该用例的 `timeout_s` 提到 ≥ 90 s，或改夹具为小图）。

## 7. 下一步（上游优先）

| 顺序 | 任务 | 入口 / 必读 | 前置核销要点 |
|---|---|---|---|
| 1 | **T-A2**（`1-3` §⑤~§⑦） | 新增 5 个 `CG_RAG_MILVUS_*` 字段 + `vector_store` 的 `Literal` 加 `milvus` + `factory` 延迟导入分支 + `.env.example` | ⚠️ 必须按 `1-2 §⑪.1` 修正 `1-3 §⑥` 的顶层 import 片段；`milvus_timeout_s` 已有真实消费方（本适配器已透传 `timeout`） |
| 2 | **T-A3**（`1-4`） | Milvus standalone（etcd + minio + milvus）可选 profile | 本机 Docker daemon 当前**未启动**；Windows 只能用 `server` 模式 |
| 3 | **T-A4**（`1-5`） | 集成测试 7 条断言（独立 collection + 用例后清理） | 卡内已落 4 条（重合度 / 幂等 / 分数口径 / 三路过滤）；其余断言补齐后，**在真机 server 上复核 COSINE 口径**（见 §2-#1） |
| 4 | **T-A9**（`1-8`） | `SectionAwareChunker`（C 组，可砍） | 与 Milvus 线无依赖 |

## 8. 交接摘要（`AGENTS.md §3.5` 的 7 项）

1. **goal 目标与状态**：T-A1 / 组件卡 C5 → ✅ 完成（无未决）。
2. **已完成任务 ID + 改动文件清单**：见 §1（3 改 + 2 新建 + `uv.lock`）。
3. **实测事实（带命令）**：`python -c "import pymilvus; print(pymilvus.__version__)"` → `3.0.0`（**系统 Python**，`.venv` 内无）；
   `pytest` 收集 **340**；关键符号：`rag_core.adapters.MilvusVectorStore`（PEP 562 导出）、`_PK_FIELD`、`_score_from_distance`。
4. **关键设计决策**：① 分数口径反向映射（1-cos → 相似度）② 不建标量索引 ③ PEP 562 懒导出（**T-A2 不得改回顶层急切导入**）
   ④ 已存在集合必须 `load` ⑤ `timeout_s` 必须透传。
5. **阻塞与未决**：无阻塞；未决 = 真机 server 上的 COSINE 口径复核（T-A3 / T-A4）。
6. **下一步第一件事**：在 `packages/rag-core/rag_core/config.py` 的"向量库"分组末尾追加 5 个 `milvus_*` 字段（`1-3 §⑤` 已给逐字片段）。
7. **临时文件清单**：本轮临时产物（探针 `*.db`、跑测日志、pytest basetemp）**已全部清理**；
   `.tmp_t/` 下他人遗留的 `full*.txt` **未动**；`文件集合/codex/2.C1~2.C3*.md` 为他人未提交文档，**未动**。

## 附录 · 可复制命令

```powershell
# ① 升级路径自证：缺 pymilvus 时默认路径照常
D:\aanacoda1\python.exe -c "import sys; sys.modules['pymilvus']=None; import rag_core, rag_core.factory as f; print(type(f.build_service_from_settings()).__name__)"

# ② 卡内用例：离线（17 passed / 4 skipped）
D:\aanacoda1\python.exe -m pytest packages/rag-core/tests/test_milvus_store.py -q

# ③ 集成用例：用 milvus-lite 本地文件当后端（21 passed）—— 真机则填 http://localhost:19530
$env:CG_RAG_TEST_MILVUS_URI = "$pwd\.tmp_t\milvus_it.db"
D:\aanacoda1\python.exe -m pytest packages/rag-core/tests/test_milvus_store.py -q

# ④ 唯一硬门禁（全量，见 AGENTS.md §4.1）
$env:TEMP = "$pwd\.tmp_t"; $env:TMP = "$pwd\.tmp_t"
D:\aanacoda1\python.exe -m pytest -q --basetemp="$pwd\.tmp_t\bt" -p no:cacheprovider
```
