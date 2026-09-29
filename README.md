# Contract-Guard-AI · 企业合同智能审核与风险预警系统

> 基于大模型的企业合同智能审核与风险预警系统（赛题【A24】· 网新恒天）
> 架构风格：**Polyglot Monorepo（多语言单仓）** —— 前端 `pnpm workspace` + 后端 / AI 引擎 `uv workspace`

---

## 1. 架构总览

```
contract-guard-ai/
├── .github/workflows/   # CI/CD：schema-check（Spectral + CodeGen 对比）、deploy（Docker）
├── packages/            # 共享核心模块
│   ├── schema/          # 【SSOT 核心】openapi.yaml + 代码生成脚本
│   ├── ai-engine/       # AI Agent 逻辑包（auditor / rewriter / conflict + memory + rag）
│   └── doc-processor/   # 文档解析与坐标抽取（layout / ocr / parser / aligner）
├── apps/                # 可运行应用
│   ├── api-gateway/     # FastAPI 主 REST API
│   ├── async-worker/    # Celery 异步任务节点（解析 / 审核 / 报告渲染）
│   └── web-ui/          # 前端 SPA（React + Vite + TS）
├── docker/              # 一键部署：compose + Dockerfile + nginx
├── pnpm-workspace.yaml  # 前端工作区
└── pyproject.toml       # Python（uv）工作区
```

### 工程约束（源自 `文件集合/report/data_scheme.md`）
- **单一真理源（SSOT）**：`packages/schema/openapi.yaml` 为权威。TS 类型 / Zod / Pydantic DTO 一律由 CLI 生成，业务代码只 `import`，禁止手写漂移。
- **前后端分离 + AI 解耦 + 异步隔离**：前端 SPA、后端 API、AI 引擎、异步 Worker 各自独立包。
- **命名**：API 字段 `camelCase`、URL Path `kebab-case`；布尔前缀 `is/has/can/should`；集合空返回 `[]` 非 `null`。
- **时间/精度**：时间 ISO 8601 UTC；ID 为 `string`；金额 `Money`（string 定点 + 整数分）；百分比 `[0,1]`。
- **幂等 / 乐观锁**：写操作 `X-Request-ID` 幂等；可编辑实体 `version` + `updatedAt` 乐观锁。
- **统一包络**：所有响应 `code` 字符串枚举 + `data` + `traceId` + `serverTime`。

---

## 2. 快速开始

### 2.1 环境准备
- Node ≥ 20（含 pnpm ≥ 9）
- Python ≥ 3.11（含 uv ≥ 0.4）
- Docker & Docker Compose（基础设施与部署）

### 2.2 安装依赖
```bash
# 前端
pnpm install

# 后端（uv 工作区，聚合所有 Python 包）
uv sync --all-packages
```

### 2.3 打通 SSOT 代码生成管道
```bash
pnpm --filter @contract-guard/schema gen
# 自动产出：
#   apps/web-ui/src/api/generated/types.ts   (openapi-typescript)
#   apps/web-ui/src/api/generated/zod.ts     (openapi-zod)
#   apps/api-gateway/app/generated/schemas.py (datamodel-code-generator)
```

### 2.4 一键 Docker 启动
```bash
docker compose -f docker/docker-compose.yml up -d --build
```

启动后可访问：

```text
后端 API      http://localhost:8000
Web 容器      http://localhost:5173
顶层 nginx    http://localhost:8080
MinIO Console http://localhost:9001
```

顶层 nginx 负责 `/api` 反代到 API，并将根路径和 `/contracts/{id}/audit` 等动态路由的回退请求转发到 Web SPA。

> ⚠️ 若已有无法回填的历史反馈数据，Alembic 会按设计拒绝静默迁移；此时应使用 fresh volume，或在明确备份后执行单独的数据清理任务。

### 2.5 前端本地开发
```bash
pnpm --filter @contract-guard/web-ui dev
```

Vite 默认地址为 `http://localhost:5173`，开发代理 `/api` 指向 `http://localhost:8000`。前端页面统一通过 `apps/web-ui/src/api/services.ts` 访问真实网关，不再使用 Mock 数据源。

---

## 3. CI / CD
- `.github/workflows/schema-check.yml`：提交 `openapi.yaml` 变更时自动跑 **Spectral 校验** + **CodeGen 产物对比** 门禁。
- `.github/workflows/deploy.yml`：构建 `Dockerfile.api` / `Dockerfile.worker` / `Dockerfile.web` 并发布。

详见各包内 `pyproject.toml` / `package.json` 与 `docker/` 目录。
