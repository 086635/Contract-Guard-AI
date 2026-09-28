# Web UI 当前进度与 API 联调状态

> 文档用途：为后续开发者、联调人员和项目维护者提供一份可直接交接的前端状态总览。本文档记录当前 `apps/web-ui` 的页面架构、API 接入情况、已完成能力、验证结果、已知限制和后续建议。
>
> 最近更新：2026-08-29

## 1. 项目当前结论

当前 Web UI 已完成从演示型页面到 API 驱动页面的第一阶段改造。

目前已经实现：

- React 页面按业务域分层组织；
- 所有网关请求集中经过统一 Axios Client；
- 工作台、审核工作台、报告、规则配置和反馈中心均已接入 FastAPI 网关；
- 上传弹窗已经从本地 `setTimeout` 模拟切换为预签名上传、任务创建和 SSE 进度流程；
- 审核页的 AI 改写、批注提交和报告生成按钮已经连接对应接口；
- API 加载、失败、空数据和重试状态已经补充；
- 前端保持浅色法务工作台视觉方向；
- 生产构建和 TypeScript 类型检查均已通过。

当前仍属于“前端 + FastAPI Mock 网关联调阶段”，还不是完整生产环境联调。后端目前使用 `MockDataFactory` 内存数据，真实数据库、MinIO、OCR、LLM 和异步 Worker 尚未接入本次前端验证范围。

## 2. 前端技术基座

当前前端使用：

- React 18
- TypeScript
- Vite
- React Router 6
- Axios
- Zustand
- Zod / OpenAPI 生成类型
- 原生 CSS + 业务域样式

接口类型来源于：

```text
src/api/generated/types.ts
src/api/generated/zod.ts
```

这两个文件属于生成文件，不建议手工修改。接口字段发生变化时，应从 OpenAPI 契约重新生成类型。

## 3. 前端目录架构

当前组件按照应用层、基础 UI 层和业务域拆分，避免组件全部平铺在同一个目录。

```text
apps/web-ui/
├── .env.example                         环境变量示例
├── DEVELOPMENT.md                       前端开发约定与设计说明
├── INTEGRATION_STATUS.md                本文档：当前进度与联调状态
├── package.json                          Web UI 脚本与依赖
├── vite.config.ts                       Vite 配置及 API 代理
├── src/
│   ├── api/
│   │   ├── client.ts                    全局 Axios Client
│   │   ├── services.ts                  业务 API 服务层
│   │   └── generated/
│   │       ├── types.ts                 OpenAPI 生成 TypeScript 类型
│   │       └── zod.ts                   OpenAPI 生成运行时 Schema
│   ├── components/
│   │   ├── layout/
│   │   │   └── AppShell.tsx             左侧导航、顶部栏、Outlet 壳层
│   │   ├── ui/
│   │   │   ├── Icon.tsx                 图标系统
│   │   │   ├── Badge.tsx                状态与风险标签
│   │   │   ├── Button.tsx               统一按钮
│   │   │   └── SectionHeader.tsx        页面标题区
│   │   ├── contracts/
│   │   │   ├── ContractRow.tsx           合同列表行
│   │   │   └── UploadPanel.tsx            合同上传弹窗与任务进度
│   │   ├── audit/
│   │   │   ├── ElementsPanel.tsx         抽取要素与风险目录
│   │   │   ├── PdfStage.tsx              PDF/文档预览区域
│   │   │   └── RiskPanel.tsx              风险摘要与审核操作
│   │   └── chat/
│   │       └── AgentChat.tsx              AI 条款改写面板
│   ├── router/
│   │   └── index.tsx                    路由配置
│   ├── views/
│   │   ├── Dashboard.tsx                工作台
│   │   ├── Audit.tsx                    合同审核工作台
│   │   ├── Report.tsx                   审核报告
│   │   ├── Settings.tsx                 规则与角色配置
│   │   └── Feedback.tsx                 人工反馈中心
│   ├── stores/                           状态管理预留区域
│   ├── App.tsx                           应用入口
│   └── index.css                         全局浅色主题与业务样式
└── public/                               静态资源
```

## 4. API 接入架构

所有业务页面都应该通过 `src/api/services.ts` 调用接口，不要在页面组件中直接新增 `fetch` 或 `axios.create()`。

请求链路如下：

```text
页面容器 / 业务组件
        ↓
src/api/services.ts
        ↓
src/api/client.ts
        ↓
Vite /api 代理或 VITE_API_BASE_URL
        ↓
FastAPI /api/v1 网关
```

`client.ts` 当前负责：

- 设置默认 API Base URL；
- 自动注入 `X-Request-ID`；
- 统一请求超时；
- 读取后端 `ApiError.message`；
- 开发环境输出 `traceId`；
- 将错误继续抛给页面容器处理。

后端成功响应统一采用：

```json
{
  "code": "SUCCESS",
  "traceId": "trace-id",
  "serverTime": "2026-08-29T00:00:00Z",
  "data": {}
}
```

`services.ts` 中的 `unwrap()` 负责读取 `data`，业务页面拿到的是具体数据，而不是重复处理响应包络。

## 5. 已接入的 API 清单

### 5.1 合同域

| 前端服务 | 方法 | 接口 | 用途 |
|---|---|---|---|
| `contractApi.list` | GET | `/contracts` | 工作台合同列表 |
| `contractApi.detail` | GET | `/contracts/{contractId}` | 合同详情 |
| `contractApi.elements` | GET | `/contracts/{contractId}/elements` | 合同要素抽取 |
| `contractApi.risks` | GET | `/contracts/{contractId}/risks` | 风险列表，可按级别过滤 |
| `contractApi.conflicts` | GET | `/contracts/{contractId}/conflicts` | 跨条款矛盾 |
| `contractApi.seals` | GET | `/contracts/{contractId}/seals` | 印章与签名合规 |
| `contractApi.parseStatus` | GET | `/contracts/{contractId}/parse-status` | 合同解析状态 |
| `contractApi.commit` | POST | `/contracts/{contractId}/commit` | 批注采纳与版本提交 |

### 5.2 上传与任务域

| 前端服务 | 方法 | 接口 | 用途 |
|---|---|---|---|
| `uploadApi.presign` | POST | `/contracts/presign` | 获取预签名上传地址 |
| `uploadApi.directUpload` | PUT | 后端返回的 `presignedUrl` | 文件直传 MinIO |
| `uploadApi.submit` | POST | `/contracts/upload` | 创建解析任务 |
| `taskApi.get` | GET | `/tasks/{taskId}` | 查询任务状态 |
| `taskApi.subscribe` | GET/SSE | `/tasks/{taskId}/stream` | 订阅任务进度 |

上传流程：

```text
选择 PDF / DOCX
      ↓
POST /contracts/presign
      ↓
PUT presignedUrl 直传文件
      ↓
POST /contracts/upload
      ↓
GET /tasks/{taskId}/stream
      ↓
任务完成后进入审核工作台
```

SSE 服务层同时兼容两种事件格式：

1. 当前 Mock 后端使用的裸 `TaskProgress`；
2. 后续网关可能使用的统一 `data` 包络格式。

### 5.3 报告域

| 前端服务 | 方法 | 接口 | 用途 |
|---|---|---|---|
| `reportApi.get` | GET | `/report/{contractId}` | 获取报告元信息与下载地址 |
| `reportApi.generate` | POST | `/report/{contractId}/generate` | 创建报告生成任务 |

当前已注册路由：

```text
/report
/report/:contractId
```

审核页生成报告后会跳转到带合同 ID 的详情路由。

### 5.4 规则、反馈与 Agent 域

| 前端服务 | 方法 | 接口 | 用途 |
|---|---|---|---|
| `settingsApi.list` | GET | `/settings/rules` | 读取合同类型规则 |
| `settingsApi.get` | GET | `/settings/rules/{configId}` | 读取单条规则 |
| `settingsApi.update` | PUT | `/settings/rules/{configId}` | 更新规则配置 |
| `feedbackApi.create` | POST | `/feedback` | 创建人工标注反馈 |
| `agentApi.rewrite` | POST | `/agent/rewrite` | 生成条款改写 Diff |

## 6. 页面完成情况

### 工作台 `Dashboard.tsx`

已完成：

- 调用合同列表 API；
- 映射合同类型和状态；
- 搜索合同名称与合同 ID；
- 状态筛选；
- API 加载状态；
- API 失败状态；
- 错误重试；
- 空结果状态；
- 刷新数据；
- 上传新合同入口；
- 点击合同进入 `/audit/:contractId`。

### 审核工作台 `Audit.tsx`

已完成并行读取：

- 合同详情；
- 抽取要素；
- 风险列表；
- 跨条款矛盾；
- 印章与签名；
- 解析状态。

已完成审核交互：

- 风险推理过程展开 / 收起；
- AI 条款改写；
- 改写结果 Diff 展示；
- 提交批注确认；
- 生成审核报告；
- 加载失败后的重试提示；
- 操作进行中的禁用状态；
- 操作完成后的 Toast 反馈。

### 上传弹窗 `UploadPanel.tsx`

已完成：

- 拖拽上传；
- 点击选择文件；
- PDF / DOCX 校验；
- 50 MB 大小限制；
- 预签名请求；
- 文件直传；
- 解析任务创建；
- SSE 进度展示；
- SSE 断开后的轮询兜底；
- 失败提示；
- 任务完成后进入审核工作台；
- 弹窗关闭时清理 SSE 连接。

### 报告 `Report.tsx`

已完成：

- 合同详情 API；
- 报告元信息 API；
- 报告状态展示；
- 健康分展示；
- 风险分级统计；
- 重点风险清单；
- 下载地址跳转；
- 报告加载失败提示；
- 动态合同报告路由。

### 规则配置 `Settings.tsx`

已完成：

- 规则列表读取；
- 规则版本与更新时间展示；
- 规则启用状态交互；
- 规则描述展示；
- 风险阈值展示；
- 保存中状态；
- 保存成功状态；
- 更新规则 API。

### 反馈中心 `Feedback.tsx`

已完成：

- HITL 统计展示；
- 最近反馈展示；
- 反馈搜索界面；
- 提交反馈按钮；
- 提交成功状态。

## 7. 当前验证结果

### 前端构建

已通过：

```bash
pnpm --filter @contract-guard/web-ui lint
pnpm --filter @contract-guard/web-ui build
```

结果：

- TypeScript 类型检查通过；
- Vite 生产构建通过；
- 106 个模块转换成功；
- `dist/index.html` 和静态资源正常生成。

### 网关接口验证

当前 FastAPI 网关已启动并完成接口验证：

```text
GET  /health                                      200
GET  /api/v1/contracts                             200 SUCCESS
GET  /api/v1/contracts/{contractId}                200 SUCCESS
GET  /api/v1/contracts/{contractId}/elements      200 SUCCESS
GET  /api/v1/contracts/{contractId}/risks         200 SUCCESS
GET  /api/v1/contracts/{contractId}/conflicts      200 SUCCESS
GET  /api/v1/contracts/{contractId}/seals          200 SUCCESS
GET  /api/v1/contracts/{contractId}/parse-status   200 SUCCESS
GET  /api/v1/tasks/{taskId}                       200 SUCCESS
GET  /api/v1/tasks/{taskId}/stream                200 event-stream
GET  /api/v1/report/{contractId}                  200 SUCCESS
GET  /api/v1/settings/rules                       200 SUCCESS
GET  /api/v1/settings/rules/{configId}            200 SUCCESS
POST /api/v1/contracts/presign                     200 SUCCESS
POST /api/v1/contracts/upload                      202 SUCCESS
POST /api/v1/contracts/{contractId}/commit         200 SUCCESS
POST /api/v1/report/{contractId}/generate          202 SUCCESS
POST /api/v1/feedback                              201 SUCCESS
POST /api/v1/agent/rewrite                         200 SUCCESS
PUT  /api/v1/settings/rules/{configId}            200 SUCCESS
```

### 当前页面验证

已在本地浏览器查看：

```text
http://127.0.0.1:5173/
http://127.0.0.1:5173/audit/00000000-0000-0000-0000-000000000001
http://127.0.0.1:5173/report/00000000-0000-0000-0000-000000000001
http://127.0.0.1:5173/settings
http://127.0.0.1:5173/feedback
```

工作台能够加载后端合同数据，审核页能够显示 API 风险数据，报告详情动态路由能够正常访问。

## 8. 当前已知限制

### 8.1 Mock 后端不是真实生产服务

当前后端由 `MockDataFactory` 提供静态内存数据，数据不会持久化，也不会真实调用：

- 数据库；
- Redis；
- MinIO；
- OCR；
- LLM；
- RAG；
- 异步 Worker。

因此当前验证重点是前端接口路径、参数和状态流转，而不是生产数据准确性。

### 8.2 MinIO 预签名地址不可访问

Mock 后端返回的示例地址类似：

```text
https://minio.contract-guard.local/...
```

当前本机无法解析这个域名，所以真实点击上传时，预签名请求和任务创建可以成功，但文件直传可能失败。

生产环境需要由后端返回真实可访问的 MinIO / S3 预签名地址，并配置对应的 CORS、权限和过期时间。

### 8.3 Word 与 PDF 当前共用同一个下载地址

后端 `ReportData` 当前只有一个 `downloadUrl`，因此报告页的“导出 Word”和“导出 PDF”都会打开同一个地址。

如果需要真正区分格式，建议后端提供：

```text
pdfDownloadUrl
wordDownloadUrl
```

或提供：

```text
GET /report/{contractId}/download?format=pdf
GET /report/{contractId}/download?format=docx
```

### 8.4 规则启用开关目前缺少独立后端字段

当前生成的 `SystemAuditConfiguration` 没有独立的 `enabled` 字段。页面开关可以改变 UI 状态，但后端更新接口目前主要处理：

- `enabledRules`；
- `riskToleranceLevel`；
- `version`。

如果规则启停需要持久化，建议后端 Schema 增加：

```text
enabled: boolean
```

### 8.5 Agent Diff 类型需要和生成 Schema 对齐

当前 Mock 返回的 Diff 类型值与生成类型定义存在潜在不一致。页面当前不依赖这个字段，所以不影响展示；如果后续启用严格的 Zod 响应校验，建议统一后端返回值和 OpenAPI 定义。

### 8.6 运行时 Zod 校验尚未启用

`client.ts` 已预留 Zod 响应校验位置，但目前只记录 TODO，没有在每个端点自动绑定 Schema。

下一阶段可以为高风险接口增加开发环境运行时校验，优先覆盖：

- 合同详情；
- 风险列表；
- 上传任务；
- Agent 改写；
- 报告数据。

## 9. 本地启动方式

### 启动前端

```bash
pnpm --filter @contract-guard/web-ui dev
```

默认地址：

```text
http://127.0.0.1:5173/
```

### 启动后端

项目原设计命令：

```bash
uv run --package api-gateway uvicorn app.main:app --reload --port 8000
```

Vite 开发代理位于 [vite.config.ts](./vite.config.ts)：

```text
/api/* → http://localhost:8008
```

如果前端和后端分开部署，可以在 `.env` 中设置：

```env
VITE_API_BASE_URL=http://localhost:8008/api/v1
```

## 10. 后续开发建议

### 优先级 P0：完成真实文件链路

1. 接入真实 MinIO / S3；
2. 确认预签名 PUT 所需的请求头；
3. 配置 MinIO CORS；
4. 增加真实文件上传进度；
5. 验证解析失败和任务重试；
6. 解析完成后返回真实 `contractId`，不要继续依赖固定 Mock ID。

### 优先级 P0：完善真实任务状态机

1. 明确 `PENDING`、`RUNNING`、`DONE`、`FAILED` 状态；
2. 统一 SSE 事件格式；
3. 增加任务断线重连；
4. 增加任务超时提示；
5. 页面离开后恢复任务状态；
6. 把上传任务、报告任务和 AI 改写任务区分开。

### 优先级 P1：补齐审核操作的数据闭环

1. AI 改写结果加入当前草稿状态；
2. 采用修改后更新风险卡片和 PDF 文本层；
3. 提交批注后刷新合同版本；
4. 使用真实乐观锁处理 409 冲突；
5. 增加“部分成功 / 部分失败”批量提交提示；
6. 让左侧风险锚点驱动 PDF 页码和 BBox 定位。

### 优先级 P1：接入真实 PDF.js 文档层

当前 `PdfStage` 仍是可交互的视觉预览结构。后续建议拆分为：

```text
PdfDocument
├── PdfCanvas
├── PdfTextLayer
├── BBoxOverlay
└── PageToolbar
```

接入后需要支持：

- PDF 页码跳转；
- 风险 BBox 高亮；
- 要素 BBox 定位；
- 风险卡片与文档双向联动；
- 缩放状态保持；
- 文本层选中与批注。

### 优先级 P1：统一页面状态组件

建议新增基础组件目录：

```text
src/components/ui/
├── DataState.tsx
├── LoadingSkeleton.tsx
├── ErrorState.tsx
├── EmptyState.tsx
└── Toast.tsx
```

目前各页面已经有加载和错误状态，但部分实现仍分散在页面 JSX 内，后续可以统一视觉和交互行为。

### 优先级 P2：路由逐步对齐产品信息架构

当前兼容路径：

```text
/
/audit
/audit/:contractId
/report
/report/:contractId
/settings
/feedback
```

如果需要严格对齐产品设计文档，建议逐步迁移到：

```text
/contracts
/contracts/:contractId/audit
/contracts/:contractId/report
/settings/rules
```

迁移时需要同步更新：

- `router/index.tsx`；
- `AppShell.tsx` 导航；
- 合同列表跳转；
- 报告页跳转；
- 面包屑；
- 浏览器刷新与部署服务器 fallback。

### 优先级 P2：补充测试

建议增加前端测试覆盖：

- `services.ts` 请求路径和参数测试；
- 统一包络解包测试；
- API 错误映射测试；
- SSE 裸事件和包络事件测试；
- 上传失败场景测试；
- 路由参数测试；
- 风险级别映射测试；
- 报告动态路由测试。

## 11. 交接时需要注意的规则

- 只在 `src/api/services.ts` 维护业务 API 封装；
- 不要在业务页面里直接新建 Axios 实例；
- 不要手工修改 `src/api/generated/types.ts` 和 `src/api/generated/zod.ts`；
- 组件继续按业务域分类，不要把所有组件放到 `components/` 根目录；
- 页面继续使用浅色主题和现有风险语义色；
- 统一使用 `contractId` 关联合同详情、风险、要素、冲突、印章和报告；
- 后端错误要展示面向用户的 `message`，开发日志保留 `traceId`；
- 新增接口先确认 OpenAPI 契约，再补充服务层和页面状态；
- 任何长耗时任务都需要同时考虑轮询、SSE 断线和失败状态；
- 修改路由后需要同时验证直接访问和浏览器刷新。

## 12. 关键文件索引

- API Client：[src/api/client.ts](./src/api/client.ts)
- API 服务层：[src/api/services.ts](./src/api/services.ts)
- 工作台：[src/views/Dashboard.tsx](./src/views/Dashboard.tsx)
- 审核页：[src/views/Audit.tsx](./src/views/Audit.tsx)
- 报告页：[src/views/Report.tsx](./src/views/Report.tsx)
- 规则配置：[src/views/Settings.tsx](./src/views/Settings.tsx)
- 反馈中心：[src/views/Feedback.tsx](./src/views/Feedback.tsx)
- 上传弹窗：[src/components/contracts/UploadPanel.tsx](./src/components/contracts/UploadPanel.tsx)
- 风险面板：[src/components/audit/RiskPanel.tsx](./src/components/audit/RiskPanel.tsx)
- AI 改写：[src/components/chat/AgentChat.tsx](./src/components/chat/AgentChat.tsx)
- 路由：[src/router/index.tsx](./src/router/index.tsx)
- 全局样式：[src/index.css](./src/index.css)
- 原有开发说明：[DEVELOPMENT.md](./DEVELOPMENT.md)
