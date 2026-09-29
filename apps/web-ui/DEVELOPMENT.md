# Web UI 开发说明

## 本次 UI 交付

当前前端基于 `React 18 + TypeScript + Vite + React Router + Zustand`，视觉方向遵循 `UI设计.md` 的 Slate / Zinc 法务工作台规范。页面以“工作台 → 合同域 → 审核域 → 报告 / 配置 / 反馈”的业务边界组织，组件不平铺：

```text
src/components/
├── layout/       应用壳层、导航、顶部栏
├── ui/            Icon、Badge、Button、SectionHeader 等基础组件
├── contracts/    合同列表行、上传弹窗
└── audit/        要素目录、PDF 阶段、风险面板
```

## 页面与交互

- 工作台：指标卡、合同列表、状态筛选、趋势洞察、上传新合同弹窗。
- 审核工作台：左侧元素 / 条款目录，中间文档高亮预览，右侧风险摘要、推理过程、冲突提示和审核操作。
- 审核报告：报告目录、版本信息、健康分概览、高风险清单与导出入口。
- 规则配置：规则树、启用开关、基础信息、风险阈值滑块和必备条款标签。
- 反馈中心：HITL 标注统计、最近标注列表和导出入口。

页面容器已通过 `src/api/services.ts` 接入 FastAPI 网关；API 数据结构继续以 `src/api/generated/types.ts` / `zod.ts` 为准。工作台、审核、报告和规则配置页面从网关读取数据，反馈中心的提交按钮调用 `POST /feedback`，审核操作调用 commit / report / agent 端点。

## API 接入约定

1. 所有请求经过 `src/api/client.ts`，不要在业务组件中新增 `fetch` 或独立 Axios 实例。
2. 响应遵循 `{ code, traceId, serverTime, data }` 统一包络。
3. 上传流程按 `presign → MinIO 直传 → upload → task / stream` 顺序接入；上传弹窗会显示 SSE 进度、失败原因，并在完成后进入审核工作台。
4. 审核页的风险、要素、矛盾和印章数据应共享 `contractId`，点击左侧锚点时驱动 PDF 页码与 BBox 定位。
5. 所有高 / 中 / 低风险颜色沿用 `Badge` 和 `risk-dot` 语义类，不要在业务页面中重新定义颜色。

## 本地运行

```bash
pnpm --filter @contract-guard/web-ui dev
pnpm --filter @contract-guard/web-ui build
```

如果依赖目录不完整，先在可访问 npm registry 的环境执行 `pnpm install`。前端 Vite 默认将 `/api` 代理到 `http://localhost:8000`。

## 联调注意事项

- Vite 默认将 `/api` 代理到 `http://localhost:8000`；也可用 `VITE_API_BASE_URL` 指向独立网关。
- 网关 SSE `/tasks/{taskId}/stream` 当前返回裸 `TaskProgress`，服务层同时兼容统一包络和裸事件。
- MinIO 预签名 URL 由真实网关签发并必须直接可访问；不存在 `.local` 静默跳过，连接、CORS 或 2xx 失败都会在弹窗中显示。
- 后端未启动时，工作台和审核页会展示可重试的 API 错误状态，不会静默显示空白列表。

## 后续建议

- 保持页面只消费 `services.ts`，不要重新引入演示数组或第二套 Client。
- 接入 PDF.js 后，将 `PdfStage` 的演示纸张替换为 Canvas + 文本层 + BBox Overlay。
- 将 AI 改写面板拆为 `components/chat/` 域，并复用 `Button`、`Badge` 和差异标记样式。
- 将路由逐步对齐产品路径：`/contracts`、`/contracts/:contractId/audit`、`/contracts/:contractId/report`、`/settings/rules`。
