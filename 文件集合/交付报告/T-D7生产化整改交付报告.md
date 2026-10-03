# T-D7 生产化整改交付报告（批级 · T-D7 完成）

> 交付日期：**2026-10-02**（C75-A / C75-C① / C75-B / C75-C② 增量：**2026-10-03**）　分支：`codex/td7-productionization`　冻结提交：**`efa9d88`**
> 范围：`文件集合/codex/4.C74T-D7回归与文档收口.md`（T-D7-10）
> 父文档：`文件集合/codex/4-7正式部署差距与工程化整改.md` §⑥.11 / §⑦.1 / §⑧.1
> commit 范围：`79e4a05`（C44）→ C74
> **结论摘要：P0 工作包 T-D7-1~T-D7-8 均有落地与测试证据；P1 工作包 T-D7-9 四卡完成；
> C75-A 已关闭 1 项 P0 契约缺口与 P1-1 生成物漂移；C75-C① 已关闭 P1-2~P1-5；
> **C75-B 已在 prod compose（HTTPS + worker/LibreOffice + 真实 DeepSeek）上连续两轮 13/13 浏览器链路全绿，关闭 D7-I20**；
> **C75-C② 已完成工作树冻结（提交 `efa9d88`，工作树干净）、真实 PG16 迁移 upgrade/downgrade 演练、
> 独立目录 clone 镜像构建、生成物哈希零漂移，关闭 D7-I19。**
> **T-D7 十个工作包（T-D7-1 ~ T-D7-10）全部完成，本报告声明「T-D7 完成」**；残留 R1（worker 2 GiB 上限
> OOMKilled）与 R2（Qdrant collection 未建致检索降级）为生产容量/数据面问题，已登记归属 C67 / C55。
> 本报告是交付说明，不是实现依据。

---

## 0. 一页速览

| 工作包 | 主题 | 状态 | 关键证据 |
|---|---|---|---|
| T-D7-1 | 认证 / RBAC / 账号模型 | ✅ 完成（C75-A 关闭 P0-1） | C44~C48 + C75-A；owner 迁移/IDOR 404/PG16 验证 |
| T-D7-2 | 审计事件与查询 | ✅ | C49~C50；append-only + 脱敏 + ADMIN-only |
| T-D7-3 | 统一错误 / 幂等 | ✅ | C51~C52；错误矩阵 + Redis 幂等（重放/409/503） |
| T-D7-4 | 生产 LLM / RAG 语义门禁 | ✅ | C53~C55；真实 DeepSeek Diff、bge-m3 召回、精排对比 |
| T-D7-5 | DOCX→PDF 预览与坐标 | ✅ | C58~C61；转换队列、坐标重建、PDF.js 真实查看器 |
| T-D7-6 | 任务可靠性 / SSE 鉴权 | ✅ | C62~C65；acks_late、stale 回收、stream-token、前端重连 |
| T-D7-7 | 上传安全 | ✅ | C56~C57；大小/类型/key、magic sniff、ClamAV |
| T-D7-8 | 生产部署与运维 | ✅ | C66~C69；secrets、prod overlay、TLS/限流、备份恢复演练 |
| T-D7-9 | 产品闭环（P1） | ✅ | C70~C73；标准条款库、双格式导出、Zod 运行时校验 |
| T-D7-10 | 发布冻结与浏览器验收 | ⚠️ **未闭环** | C74 完成回归/收口；C75-A 已闭环；**C75-B 全量 13 条与 C75-C 冻结待执行** |

---

## 1. 方法与口径

- 全部结论带命令、日期、commit（见 §5/§7）；未实测项一律标 `⏳`，不写通过。
- 只统计**可复现**证据：离线用例 + 真实容器/真实 PG 演练 + 构建产物。
- 失败/跳过逐条解释（§7.3、§7.4）；P0 未闭环项集中在 §8，不隐藏。

---

## 2. P0 工作包证据矩阵（T-D7-1 ~ T-D7-8）

| 工作包 | 最低断言（4-7 §⑦.1） | 实测证据 |
|---|---|---|
| T-D7-1 | 无 token 401；无权限 403；登录/刷新/登出/角色矩阵；生产缺 secret 启动失败 | C44~C48 卡片回填；`test_auth_routes.py` / `test_auth_service.py` / 前端 `authStore.test.ts`；真实 PG 往返（C45/C46/C47 报告） |
| T-D7-2 | 登录/上传/查看/改写/提交/导出/设置均产生审计；metadata 不含原文/密钥；仅 ADMIN 可查 | C49/C50 回填；`test_audit_service.py` / `test_audit_routes.py`（append-only、脱敏、分页、ADMIN-only） |
| T-D7-3 | 401/403/404/409/422/503 全为 ApiError；幂等重放不重复写；冲突 409；Redis 故障 503 | C51/C52 回填；错误矩阵用例 + 真实 Redis 幂等/503 |
| T-D7-4 | 生产拒绝 offline/hash/noop；真实模型 Diff；失败任务 FAILED；召回达标 | C53~C55 回填；真实 DeepSeek rewrite Diff、bge-m3 health、Recall/MRR 门禁 PASS |
| T-D7-5 | PDF 与 DOCX 均真实页；页码/BBox 一致；转换失败可重试且原文不丢 | C58~C61 回填；真实 soffice 转换（C59 门禁）+ BBox 重建断言 + PDF.js 查看器用例 |
| T-D7-6 | worker kill 后可重试/失败收敛；stale 回收；stream token 可通过且越权拒绝 | C62~C65 回填；Celery 可靠性策略用例 + reconciler 用例 + stream-token 越权 403 |
| T-D7-7 | 绕过前端上传非法文件被拒；大小/类型/ETag/AV 任一失败不进 parse | C56/C57 回填；网关上传安全用例 + 真实 MinIO stat + ClamAV 镜像门禁 |
| T-D7-8 | 空机 Compose 启动；仅 80/443 暴露；TLS/健康/备份/恢复通过；无默认口令 | C66~C69 回填；prod overlay 契约用例、真实 `nginx -t`、真实 PG+MinIO 备份恢复演练 |

---

## 3. P1 工作包（T-D7-9）

| 卡 | 主题 | 证据 |
|---|---|---|
| C70 | 标准条款库数据与服务 | 新增 8 passed；真实 PG 迁移 `20261002_0013` + CRUD 往返 |
| C71 | 标准条款库管理页 | 新增 5 passed（前端合计 28/7 files）；`tsc` + `vite build` |
| C72 | 报告独立 Word/PDF 导出 | 新增 11 passed；真实 PG 迁移 `20261002_0014`；worker 容器 DOCX 魔数 + PDF 降级 |
| C73 | Zod 运行时响应校验 | 新增 5 passed；operationId 注册表 30 端点；生产拒绝脏数据 |

---

## 4. T-D7-10 发布冻结（本卡状态）

| 冻结项 | 状态 | 证据 / 说明 |
|---|---|---|
| 全量 pytest ≥ 基线 | ✅ | 见 §7.1（**897 passed / 53 skipped / 0 failed**；非沙箱完整门禁） |
| 工作树冻结（D7-I19） | ✅ | 冻结提交 **`efa9d88`**；`git status` 干净；`docker/secrets/` 已入 `.gitignore`，提交不含 secrets/.env |
| 迁移 upgrade/downgrade 演练 | ✅ | 真实 **PG16** `test_postgres_upgrade_downgrade` passed（2.93s）+ SQLite 四段式 |
| 独立 clone 镜像构建 | ✅ | 独立目录 clone（HEAD=`efa9d88`，worktree 干净）→ compose config exit 0 → `contract-guard-api` / `contract-guard-worker` 镜像构建 exit 0 |
| OpenAPI 生成物零漂移 | ✅ | `tests/release/test_generated_artifacts.py::test_openapi_generation_drift`（35 operationId / 31 路径 1:1） |
| 生成物哈希登记 | ✅ | §6 哈希表（`test_generated_hash_stable` 校验） |
| 组件卡状态与代码一致 | ✅ | `test_all_td7_cards_have_evidence_or_pending`（32 张卡逐卡核销） |
| 文档同步（模块总纲 / 0.§0.10 / 4-7） | ✅ | `test_docs_reference_4_7` / `test_docs_links_and_status` |
| e2e_smoke 覆盖 T-D7 路径 | ✅（离线守卫） | `test_e2e_smoke_td7_paths` + `e2e_smoke.py --help` exit 0；**真实链路执行归 C75** |
| 干净 checkout 可重复构建 | ✅ | `docker compose config`（base + prod overlay）exit 0；`test_clean_checkout_build` 通过（release 守卫 8 passed） |
| Playwright 浏览器主链路 | ✅ **已闭环（D7-I20 关闭）** | prod compose（8443 HTTPS + worker/LibreOffice + 真实 DeepSeek）**13/13 passed 连续两轮（5.2 min，workers=1）**；`playwright-report/results.json` sha256 `c647416d85cf`（13/0/0/0） |

---

## 5. 组件卡逐卡核销（C44 ~ C75）

| 卡 | commit | 关键证据（摘要） | 状态 |
|---|---|---|---|
| C44 | `79e4a05` | OpenAPI 认证契约 + users/auth_sessions 迁移 | ✅ |
| C45 | `68a65df` | Argon2/JWT/refresh 轮换、真实 PG 会话 | ✅ |
| C46 | `b477003` | AuthService、锁定/管理员保护 | ✅ |
| C47 | `5550571` `0b58c3f` | 认证/RBAC 路由与依赖、锁定态持久化 | ✅ |
| C48 | `e33774b` | 前端登录态、路由守卫、内存 access token | ✅ |
| C49 | `45db883` | audit_events 模型/迁移/仓储（append-only） | ✅ |
| C50 | `a1b6841` | 审计采集点 + ADMIN 查询 API | ✅ |
| C51 | `61e7cdf` | 全局 ApiError + 校验映射 | ✅ |
| C52 | `1ef3dc9` | Redis 幂等中间件 + 前端错误适配 | ✅ |
| C53 | `f49487e` | 生产配置校验与启动门禁 | ✅ |
| C54 | `2ff5bcb` | 真实 LLM Provider 与失败语义 | ✅ |
| C55 | `997db66` | 真实 embedding/rerank 健康与评测门禁 | ✅ |
| C56 | `121afdc` | 上传大小/类型/key 服务端约束 | ✅ |
| C57 | `9daf656` `f97d670` | magic sniff + ClamAV 扫描 | ✅ |
| C58 | `1be069e` | 预览元数据表 + 预览 API | ✅ |
| C59 | `c1fe009` | DOCX→PDF convert 任务与队列 | ✅ |
| C60 | `1351e39` | 预览坐标重建 / BBox 映射 | ✅ |
| C61 | `917a28e` `a6b9619` | PDF.js 真实查看器 + document 泄漏修复 | ✅ |
| C62 | `7c136ba` | 任务可靠性字段 + Celery 策略（修复 SCAN/CONVERT 落库） | ✅ |
| C63 | `bfa0e0b` | stale 任务回收 + worker 心跳对账 | ✅ |
| C64 | `4232aec` | SSE stream-token（作用域 + TTL） | ✅ |
| C65 | `eb5eb05` | 前端 SSE 换 token 重连 + 2s 轮询兜底 | ✅ |
| C66 | `a7c7c12` | Docker secret 注入 + SecretStr 掩码 | ✅ |
| C67 | `ed0cc40` | prod overlay + `health/live` / `health/ready` | ✅ |
| C68 | `91d2ca0` | nginx TLS/安全头/限流/SSE 不缓冲（真实 `nginx -t`） | ✅ |
| C69 | `9f02531` | PG/MinIO 备份 + 恢复演练脚本（真实 PG+MinIO 演练） | ✅ |
| C70 | `4da6f54` | 标准条款库数据与服务（真实 PG 迁移/CRUD） | ✅ |
| C71 | `2197078` | 标准条款库管理页（Settings 真实数据） | ✅ |
| C72 | `bbb85ba` | 报告双格式导出（DOCX/PDF 独立下载） | ✅ |
| C73 | `c413936` | Zod 运行时响应校验（operationId 注册表） | ✅ |
| C74 | 本卡 | 批级回归 + 文档收口 + release 守卫测试 | ✅ |
| C75 | — | Playwright E2E + 正式部署验收 | ✅ **T-D7-10 闭环** |
| C75-A | 本轮工作树（待提交） | contracts 归属字段/0015 迁移/IDOR 404/OpenAPI 生成物 | ✅ |
| C75-C① | 本轮工作树（待提交） | 守卫空转修复+负例自证 / 转换任务行+扫描拒绝 FILE_REJECTED 审计 / reconciler compose 旁车 / 前端包体评估 | ✅ |
| C75-B | 本轮工作树（待提交） | prod compose 13/13 浏览器链路（全绿×2）+ 链路 6 判因 + 干净构建 + 证据归档；关闭 D7-I20 | ✅ |
| C75-C② | `efa9d88`（冻结提交） | 工作树提交冻结（60 文件，排除 secrets/.env）+ 真实 PG16 迁移 upgrade/downgrade 演练 + 独立目录 clone 镜像构建 + 生成物哈希零漂移 + release 守卫 8 passed；关闭 D7-I19 | ✅ |

---

## 6. 生成物哈希（C74 冻结登记）

命令：`python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()[:12])"`（2026-10-02）

| 产物 | sha256（前 12 位） |
|---|---|
| `packages/schema/openapi.yaml` | cf439d599bd0 |
| `apps/api-gateway/app/generated/schemas.py` | d257abe8af5c |
| `apps/web-ui/src/api/generated/types.ts` | a75069a764cf |
| `apps/web-ui/src/api/generated/zod.ts` | 96260b88d7fa |

> 生成物被 `.gitignore` 忽略：干净 checkout 需先 `pnpm --filter @contract-guard/schema gen`；
> `tests/release/test_generated_hash_stable` 会在哈希漂移时报错并要求重新登记。

---

## 7. 全量回归门禁

### 7.1 pytest（2026-10-03，`D:\aanacoda1\python.exe -m pytest --basetemp=... -p no:cacheprovider`）

```text
891 passed, 52 skipped in 171.16s        # C75-A（2026-10-03）
897 passed, 53 skipped in 182.04s        # C75-C①（2026-10-03）
```

### 7.1b Playwright 浏览器验收（2026-10-03，`CG_E2E_BASE_URL=https://localhost:8443`）

```text
13 passed (5.2m)   — 连续两轮；结果与证据：apps/web-ui/playwright-report/results.json
                     （expected=13 / unexpected=0 / flaky=0 / skipped=0 / duration=310.2s；
                       sha256= c647416d85cf…）
```

链路 6 判因为**环境**（HTTP 下 `refresh` cookie `secure=True` 不发 + 短链路连续登录撞 nginx `cg_login` 5 次/分限流 → 503），
非业务代码缺陷；改用 8443 HTTPS 入口 + E2E 登录节流后稳定通过。

（C74 基线 877/52/2 → C75-A 新增 8 条离线归属/路由/迁移测试，并复用 PG16 集成 5 passed；
C75-C① 新增 6 条离线用例 + 1 条 PG-gated 扫描拒绝审计端点用例，skip 52→53 属设计内；非沙箱完整门禁 0 failed。
C75-C① 另在真 PG16 上执行 `test_scan_reject_audit_query_admin_endpoint`（`GET /api/v1/audit/events?action=FILE_REJECTED`，total=1）。）

### 7.2 其它门禁

| 门禁 | 结果 |
|---|---|
| 前端 `tsc --noEmit -p tsconfig.app.json` | ✅ exit 0 |
| 前端 `vitest run --root .` | ✅ 28 passed / 7 files |
| 前端 `vite build` | ✅ 通过（JS 439.06 kB / gzip 135.29 kB） |
| ruff（改动文件） | ✅ 无新增 |
| Spectral（openapi.yaml） | 151 problems（60 errors / 91 warnings，与 C70 基线一致） |
| `uv sync --frozen --all-packages` | ✅ 通过（C70 新增 rag-core 直接依赖后） |
| release 守卫测试（本卡新增） | ✅ 见 §4 |

### 7.3 关于 2 条 failed（环境假红）

| 用例 | 原因 |
|---|---|
| `apps/async-worker/tests/test_parse_doc_task.py::test_task_apply_pdf_native` | `data/parse/` 本机写权限被拒 |
| `apps/async-worker/tests/test_parse_pipeline.py::test_pdf_native_pipeline_end_to_end` | 同上（warnings 多一条 `PermissionError`） |

两条自 C56 起登记为本机环境假红；CI/容器内（`data/parse` 可写）应转绿，属 C75 干净环境验收范围。

### 7.4 关于 52 条 skip

全部来自**设计内的外部依赖门禁**（未设置对应 env 时按标准 §0.6.2 skip）：
Milvus 集成（`CG_RAG_TEST_MILVUS_URI`）、真实 embedding（`CG_RAG_TEST_EMBED_BASE_URL`）、
真实 rerank（`CG_RAG_TEST_RERANK_URL`）、Paddle 备用 OCR 线（`CG_OCR_TEST_PADDLE`）、
真实 PG 仓储类用例（`CG_POSTGRES_DSN`）、真实 MinIO（`CG_MINIO_TEST_ENDPOINT`）、
真实 LibreOffice（`CG_CONVERT_TEST_SOFFICE`）等。**C74 未新增永久 skip**：
本卡新增的 release 守卫在生成物存在时全绿，仅当生成物缺失（干净 checkout）时显式 skip 并提示先跑 `pnpm gen`。

---

## 8. 未完成项与残余风险

### 8.1 P0（未闭环，阻塞「T-D7 全部完成」的声明）

| # | 项 | 依据 | 归属 / 建议 |
|---|---|---|---|
| P0-2 | **Playwright 浏览器 E2E（12 条主链路）与正式部署验收未执行** | `4-7 §⑥.11` D7-I20、§⑦.1 T-D7-10 | **C75**（下一卡） |

### 8.2 P1（可延期，但必须登记）

| # | 项 | 影响 | 归属 |
|---|---|---|---|
| P1-2 | `test_all_v1_routes_except_login_refresh_require_auth` 恒为空断言 | 该守卫实际未生效（路径前缀判断问题） | 测试整改（跨卡） |
| P1-3 | 转换任务未写 `audit_tasks` 行；扫描拒绝未写 `FILE_REJECTED` 审计 | 审计完整性缺口（D7-I15 期望「所有拒绝事件写审计」） | C57/C59 收尾 |
| P1-4 | `TaskReconciler` 未定时编排（无 cron/Beat/compose 服务） | stale 回收目前只能手工/脚本触发 | C63 卡片原归 C67（未做），建议并入后续运维卡 |
| P1-5 | C73 使前端包体 JS +90 kB（Zod + 30 个生成 schema） | 首屏体积增大；可用按需注册优化 | C74 后续或 C75 评估 |

### 8.3 已登记但**不阻塞**发布的风险

- 真实证书签发/HSTS/限流触发、真实 LibreOffice PDF、真实 Qdrant/生产 embedding 的浏览器级验收 → **C75**；
- 备份调度的编排（cron/Beat）→ 需在部署卡里落地（与 P1-4 同源）。

---

## 9. 结论

- **T-D7-1 ~ T-D7-8（P0）**：均有落地代码、单测/真实服务证据与卡片回填；C75-A 已关闭 P0-1（contracts 归属、迁移、IDOR、生成物），T-D7-1 完整收口。
- **T-D7-9（P1）**：C70~C73 全部完成并回填。
- **T-D7-10**：本卡完成「回归 + 生成物冻结 + 文档/卡片核销 + 批级报告」，**Playwright 与干净 checkout 验收未做** → 归 C75。
- 因此本报告**不声明 T-D7 全部完成**；C75-B/C 完成后需在本文追加「发布冻结最终确认」段落并更新 §4/§8。

C75-A 增量（2026-10-03）：contracts owner/legacy、Alembic 0015、上传写 owner、详情/任务/SSE 404、OpenAPI 生成物与哈希已闭环；离线 8 passed、PG16 5 passed、非沙箱全量 891/52/0。

C75-B 进展（历史）：Playwright 1.63.0 + Chromium 1243；`apps/web-ui/e2e/` 13 条链路 / 6 文件可枚举；真实栈跑通登录/权限/登出等单条链路；全量 13 条仍需 C75-B 收口。

---

## 附：C74 新增守卫清单

| 用例 | 文件 | 作用 |
|---|---|---|
| `test_all_td7_cards_have_evidence_or_pending` | `tests/release/test_docs_sync.py` | 32 张卡逐卡核销：文件存在 + 状态带证据或显式待实施 |
| `test_docs_reference_4_7` | 同上 | 模块总纲与每张 T-D7 卡引用父文档 4-7 |
| `test_docs_links_and_status` | 同上 | 0.§0.10 与模块总纲的 4.C* 链接全部落盘且互相覆盖 |
| `test_openapi_operation_inventory` | `tests/release/test_generated_artifacts.py` | SSOT 库存冻结（35 operationId / 31 路径） |
| `test_openapi_generation_drift` | 同上 | zod alias 与 types 路径 1:1 对齐 OpenAPI（零漂移） |
| `test_generated_hash_stable` | 同上 | 生成物 sha256 前 12 位与本文 §6 登记一致 |
| `test_clean_checkout_build` | 同上 | lock 文件/testpaths/gitignore 等可复现输入冻结 |
| `test_e2e_smoke_td7_paths` | `tests/release/test_e2e_smoke.py` | `e2e_smoke.py` 覆盖认证/幂等/预览/任务恢复，且 `--help` 可执行 |
