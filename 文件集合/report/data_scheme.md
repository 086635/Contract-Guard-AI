# 基于大模型的企业合同智能审核与风险预警系统

# 数据类型契约规范（Data Schema & API Contract）

> **文档版本**：v1.0　**状态**：基线冻结　**单一真理源（SSOT）**：本文档 + `openapi.yaml` + `types.ts` 三者由 CLI 同源生成，禁止手写漂移
> **适用范围**：前端（Vue3 + Element Plus）、后端（FastAPI）、AI Agent 引擎（LangGraph + LLM）、数据库（PostgreSQL / MinIO）全部跨端数据交换
> **配套赛题**：【A24】基于大模型的企业合同智能审核与风险预警系统（网新恒天）

---

## 0. 如何使用本契约（强制约束）

| 角色 | 必须遵循的约束 |
|---|---|
| 前端工程师 | 所有 API 调用经 `@/api/client`；响应必须经 Zod 运行时校验；UI 状态严格对应 §5.1 四态矩阵 |
| 后端工程师 | DTO 由 Schema 生成；集合字段永不为 `null`；业务错误走 `code` 枚举；写操作支持 `X-Request-ID` 幂等 |
| AI Agent / LLM 代码 | 输出必须收敛到本文 §9 领域模型；CoT 与 Citation 字段不可省略；禁止返回未声明字段 |
| AI-Coding（代码生成） | 仅可引用本文常量（`Enum`/`const`），禁止魔法值；类型由 Schema 派生，禁止凭空生成 |

**生成链路（单一真理源）**：

```
openapi.yaml (权威)
   └─> pydantic (后端 DTO)  ── CLI ──┐
   └─> types.ts  (前端类型) ── CLI ──┤─> zod schemas (运行时校验) ── CLI ──┘
```

---

## 1. 全局基础类型与通用约定（Common Types）

### 1.1 标量精度约定（强制）

| 概念 | 契约声明 | 反模式（禁止） |
|---|---|---|
| 实体 ID / Snowflake ID | `string`（如 `"18823 4567 8901 2345"` 仅作示例，实际为纯数字字符串） | `number`（>2^53 精度丢失） |
| 金额 `Money` | 见 §1.3，统一 `string` 定点小数 | JS `number` 浮点 |
| 百分比 | `number`，区间 `[0.00, 1.00]`（如 `0.5`=50%） | `0~100` 整数 |
| 时间 | `string`，ISO 8601 UTC（如 `2026-08-13T11:39:04Z`）或 13 位毫秒时间戳 `number` | `yyyy-MM-dd HH:mm:ss`（无时区） |
| 布尔 | 字段名带 `is/has/can/should` 前缀 | 无方向名词（如 `status:true`） |
| 集合 | 空数据返回 `[]`，**禁止 `null`** | `null` / 缺省键 |

### 1.2 公共枚举（禁止魔法值）

```typescript
// 合同类型（≥5 类，赛题要求）
type ContractType =
  | 'PURCHASE'        // 采购合同
  | 'SALES'           // 销售合同
  | 'NDA'             // 保密协议
  | 'OUTSOURCING'     // 服务外包合同
  | 'LABOR'           // 劳动合同
  | 'OTHER';          // 其他（需人工归类）

// 风险等级
type RiskLevel = 'HIGH' | 'MEDIUM' | 'LOW';

// 文件来源类型
type FileType = 'DOCX' | 'PDF_NATIVE' | 'PDF_SCANNED' | 'IMAGE';

// 合同整体状态（乐观锁版本链驱动）
type ContractStatus =
  | 'UPLOADING'       // 上传中
  | 'PARSING'         // 解析中（OCR/PDF）
  | 'PENDING_REVIEW'  // 待复核
  | 'IN_REVIEW'       // 复核中
  | 'FINALIZED';      // 已定稿

// 反馈标注结论
type FeedbackVerdict = 'CORRECT' | 'FALSE_POSITIVE' | 'MISSING'; // 正确/误报/漏报
```

### 1.3 基础复合类型

```typescript
// 金额：统一 string 定点小数（主单位），配套整数分用于精确计算
interface Money {
  amount: string;        // 定点小数字符串，如 "500000.00"
  currency: 'CNY' | 'USD' | 'EUR' | 'OTHER';
  amountMinorUnit: number; // 整数分（= amount*100 取整），用于无误差计算
}

// 归一化百分比（仅 0.00~1.00）
type Percentage = number; // 运行时 Zod 约束 range(0,1)

// 坐标绑定框（PDF 用户空间，72dpi，左上原点）
interface BBox {
  page: number;          // 1-based 页码
  x: number;             // 左上角 x（PDF points）
  y: number;             // 左上角 y（PDF points）
  width: number;
  height: number;
  coordinateSystem: 'PDF_POINTS';
}

// 分页包裹（所有列表接口统一）
interface Paginated<T> {
  items: T[];            // 空则 []
  total: number;         // 总数
  page: number;          // 当前页（1-based）
  pageSize: number;
  hasMore: boolean;      // 到底标记，UI 显式消费
}

// 统一成功响应包裹（所有接口）
interface ApiSuccess<T> {
  code: 'SUCCESS';
  data: T;
  traceId: string;       // 链路追踪
  serverTime: string;    // ISO 8601 UTC
}

// 统一错误响应（见 §4）
interface ApiError {
  code: ErrorCode;       // 字符串枚举，非数字
  message: string;       // 面向用户提示
  detail: string;        // 开发排查信息
  traceId: string;
}
```

---

## 2. 传参与数据类型契约规范（精细化类型边界）

### 2.1 空值与缺省语义

- **JSON 显式禁止 `undefined`**：任何响应体不得出现 `undefined` 键。
- **`null` 语义**：字段存在但值为空（如 `email: null` = "用户未填写邮箱"）。
- **键不存在语义**：仅用于**更新（PATCH）接口**——请求体中某键缺失代表"不修改该字段"；响应体不得利用此语义。
- **集合兜底**：所有 `List/Array` 字段无数据时后端**必须返回 `[]`**，前端/AI 可安全 `.map`。

```typescript
// 反例（禁止）： data: { tags: null }  →  TypeError: Cannot read properties of null
// 正例（强制）： data: { tags: [] }
```

### 2.2 大数与浮点数精度收敛

- 所有 ID（Snowflake / UUID / DB 主键）在 API 契约中声明为 `string`。
- 金额见 §1.3 `Money`（string 定点 + 整数分）。
- 百分比统一 `[0,1]` 小数，禁止 `0~100`。

### 2.3 时间与时区表达标准

- 统一 `ISO 8601 UTC` 字符串（带 `Z`）或 13 位毫秒时间戳 `number`。
- 禁止返回无时区 `yyyy-MM-dd HH:mm:ss`；前端渲染时再按用户本地时区格式化。
- 日期区间使用 `{ start: string; end: string }`（均为 UTC 字符串）。

### 2.4 多态与联合类型荷载（Discriminated Unions）

动态形态数据必须含显式鉴别器 `type`，并定义每种 `type` 的负载，便于 TS 类型保护与 AI 生成 Type Guard。

```typescript
// 审核流中推送的"消息卡片"联合类型
type AuditMessage =
  | { type: 'TEXT'; text: string }
  | { type: 'IMAGE'; url: string; bbox: BBox }
  | { type: 'DIFF'; before: string; after: string; clauseId: string }
  | { type: 'PROGRESS'; percent: number; stage: string };

// 前端类型保护（AI 生成模板）
function isDiffMessage(m: AuditMessage): m is Extract<AuditMessage, { type: 'DIFF' }> {
  return m.type === 'DIFF';
}
```

---

## 3. 命名与语义表达规范（抹平二义性）

1. **全链路命名镜像收敛**：API 字段、Query、Path 参数一律 `camelCase`；URL Path 用 `kebab-case`。**禁止** DB `snake_case` 直接透传前端。
   - ✅ `GET /api/v1/contracts/{contractId}/risk-items`
   - ❌ `GET /api/v1/contracts/{contract_id}/risk_items`
2. **布尔前缀**：`isEditable`、`hasPermission`、`canRewrite`、`shouldNotify`。
3. **批量参数范式**：入参统一 `ids: string[]` 或 `items: Array<{ id: string; ... }>`，**禁止**逗号分隔字符串 `ids="1,2,3"`。

```typescript
// 批量采纳风险修改
interface BatchCommitBody {
  ids: string[];                 // ✅
  items: Array<{ id: string; accept: boolean }>; // ✅ 备选
}
```

---

## 4. 异常流转与防重提交规范

### 4.1 业务错误码与展示文本分离

- HTTP 状态码仅表网络/系统层（200 / 401 / 403 / 429 / 500）。
- 业务异常经响应体 `code`（字符串枚举）+ `message`（用户提示）+ `detail`（排查）。
- **禁止**无意义纯数字编码。

```typescript
type ErrorCode =
  | 'SUCCESS'
  | 'ERR_VALIDATION'              // 参数校验失败
  | 'ERR_UNAUTHORIZED'            // 未登录
  | 'ERR_FORBIDDEN'               // 无权限
  | 'ERR_CONTRACT_NOT_FOUND'
  | 'ERR_OCR_FAILED'
  | 'ERR_LLM_TIMEOUT'
  | 'ERR_DATA_CONFLICT'           // 乐观锁冲突
  | 'ERR_DUPLICATE_REQUEST'       // 幂等拦截
  | 'ERR_FILE_TOO_LARGE'
  | 'ERR_UNSUPPORTED_FILE_TYPE'
  | 'ERR_FEEDBACK_ALREADY_EXISTS'
  | 'ERR_INTERNAL';
```

### 4.2 幂等性（X-Request-ID）

- 创建/支付/提交等写操作，前端 Header 携带 `X-Request-ID: <UUID>`。
- 后端据此缓存响应，重复提交返回 `ERR_DUPLICATE_REQUEST` 或首次结果。

### 4.3 乐观锁与冲突处理

- 所有可编辑实体返回携带 `version: number` 与 `updatedAt: string`。
- 提交更新需带回二者；冲突抛 `ERR_DATA_CONFLICT`。
- 前端交互规范：弹窗提供「覆盖保存 / 重新拉取」二选一。

---

## 5. 前后端功能联通与交互逻辑

### 5.1 页面状态 × API 响应映射矩阵（四态）

| 页面/组件 | Loading | Empty | Success | Error |
|---|---|---|---|---|
| 工作台历史列表 | `GET` 未返回，骨架屏 | `items: []` + 空态插画 | `items: ContractSummary[]` | `ApiError` 提示条 |
| 审核主界面左栏 | PDF 渲染 loading | 不适用 | `BBox[]` + 文本 | 解析失败重试 |
| 风险卡片列表 | 骨架卡 | `risks: []` + "暂无风险" | `risks: EnhancedRiskItem[]` | 错误卡 + 重新分析 |
| 报告预览 | 生成中 spinner | 不适用 | `Report` | 导出失败 toast |

> 约定：Empty 必须返回**结构完整的空响应**（如空数组 `[]`），而非 `null` 或 204。

### 5.2 级联请求与依赖时序图谱

```
[A] POST /contracts/upload (拿 contractId)
        │
        ├─► [B] GET /contracts/{id}/parse-status  (轮询/SSE)
        │         └─ 解析完成 ─► 并发:
        │               ├─► [C] GET /contracts/{id}/elements
        │               ├─► [D] GET /contracts/{id}/risks
        │               ├─► [E] GET /contracts/{id}/conflicts   (跨条款)
        │               └─► [F] GET /contracts/{id}/seals       (印章合规)
        │
[A] 完成后 ─► 进入 /audit/:id → 渲染 C/D/E/F
```

> 显式约束：必须等 `contractId` 返回后再并发 B/C/D/E/F；禁止在 `contractId` 未就绪时发起下游请求，杜绝 Race Condition。

### 5.3 文件上传三步法（禁止后端中转大文件）

1. 前端 `POST /api/v1/contracts/presign` → 获取 `presignedUrl` + `fileKey`。
2. 前端直传 OSS/S3（MinIO）`PUT {presignedUrl}`。
3. 前端 `POST /api/v1/contracts/upload` 回传 `{ fileKey, fileName, fileType }` 触发解析任务。

```typescript
interface PresignResponse { presignedUrl: string; fileKey: string; expiresIn: number; }
interface UploadBody { fileKey: string; fileName: string; fileType: FileType; }
```

### 5.4 长耗时任务契约

- 提交解析/重写等返回 `taskId`，前端经 **SSE**（`GET /tasks/{taskId}/stream`）或轮询（`GET /tasks/{taskId}`）获取进度。
- 进度结构：`{ taskId, stage, percent, status: 'RUNNING'|'DONE'|'FAILED', result?: T }`。

### 5.5 局部失败（Partial Success）范式

批量操作返回必须含 `successIds` 与 `failedItems`，禁止仅返回布尔值。

```typescript
interface BatchResult {
  successIds: string[];
  failedItems: Array<{ id: string; reason: string }>;
}
```

---

## 6. AI-Coding 专属防御性工程约束

1. **SSOT 类型派生**：以 `openapi.yaml` / `types.ts` 为唯一权威，前端 API 函数、Zod 校验、后端 DTO 由 CLI 生成，禁止手写或 AI 凭空生成。
2. **运行时边界断言**：HTTP 响应拦截器中用 Zod/Valibot 校验 Payload；开发环境遇脏数据（如 `null` 代替数组）立即断言报错，避免 AI 堆砌 `if-else` 兜底。
3. **禁止隐式魔法值**：状态码、动作类型、路由路径在 Schema 中定义为 `Enum`/`const`，强制引用常量对象。
4. **全局 HTTP Client 唯一注入点**：所有请求走 `@/api/client`，禁止 `fetch`/`axios.create()`/第三方请求库，保证拦截器与 Token 刷新生效。
5. **UI 兜底行为显式化**：缺失字段默认占位符（如未知姓名显示 `-`）、`hasMore` 到底标记、文本超长截断规则在文档约定，AI 不随意发挥。

```typescript
// 常量对象（AI 直接引用，禁止字面量）
export const RISK_LEVEL = { HIGH: 'HIGH', MEDIUM: 'MEDIUM', LOW: 'LOW' } as const;
export const CONTRACT_STATUS = { PARSING: 'PARSING', PENDING_REVIEW: 'PENDING_REVIEW' } as const;
```

---

## 7. 联调契约与测试验证

### 7.1 基于 Schema 的动态 Mock

- Mock 服务直接读取 `openapi.yaml` 动态生成符合类型与长度约束的随机数据，**禁止**手写静态 JSON Mock。
- 工具建议：`@faker-js/faker` + `openapi-faker` 或 Prism。

### 7.2 冒烟用例（Smoke Test Cases）

每个 API 文档必须附 ≥2 组完整 JSON：1 组全字段成功、1 组典型异常（校验失败/业务报错），作为联调与单测断言标准。示例见 §8 各端点。

### 7.3 核心端点清单（摘要）

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/v1/contracts/presign` | 获取直传凭证 |
| POST | `/api/v1/contracts/upload` | 触发解析 |
| GET | `/api/v1/contracts/{contractId}` | 合同详情 |
| GET | `/api/v1/contracts/{contractId}/elements` | 要素抽取 |
| GET | `/api/v1/contracts/{contractId}/risks` | 风险列表 |
| GET | `/api/v1/contracts/{contractId}/conflicts` | 跨条款矛盾 |
| GET | `/api/v1/contracts/{contractId}/seals` | 印章签名合规 |
| POST | `/api/v1/agent/rewrite` | AI 重写（Diff） |
| POST | `/api/v1/contracts/{contractId}/commit` | 采纳修改/新版本 |
| POST | `/api/v1/feedback` | 标注反馈 |
| GET/PUT | `/api/v1/settings/rules` | 规则与模板配置 |

---

## 8. 契约评估与补充字段规范（四大缺失维度补齐）

> **评估结论**：对照赛题与补充需求，原契约在以下 4 维度缺失，本节补齐为完整 OpenAPI/TS 契约，并附端点、示例、错误码与冒烟用例。

### 8.1 印章与签名定位及合规审查（Seal & Signature Compliance）

**业务价值**：扫描件合同中印章/签名区域的定位与合规检查（骑缝、遮挡、与主体不匹配、疑似伪造）。

```typescript
type SealType =
  | 'OFFICIAL_SEAL'                 // 公章
  | 'FINANCIAL_SEAL'                // 财务章
  | 'CONTRACT_SEAL'                 // 合同专用章
  | 'LEGAL_REPRESENTATIVE_SEAL'     // 法定代表人章
  | 'HANDWRITTEN_SIGNATURE';        // 手写签名

type SealStatus =
  | 'VALID'                         // 合规
  | 'MISSING'                       // 缺失（应有未盖）
  | 'SUSPICIOUS_OVERLAP'            // 重叠/骑缝异常
  | 'BLURRY_UNRECOGNIZABLE'         // 模糊不可辨
  | 'UNAUTHORIZED';                 // 未授权/名称不匹配

interface SealSignatureDetection {
  id: string;                       // string ID
  type: SealType;
  status: SealStatus;
  bbox: BBox;                       // 见 §1.2
  page: number;                     // 冗余页码，便于索引
  extractedText: string | null;     // 印章 OCR 出的公司名/税号；未识别为 null
  isMatchedWithParties: boolean;    // 印章名称是否与甲/乙方匹配
  complianceNote: string;           // 合规审查意见
  confidence: number;               // [0,1] 检测置信度
}

// 列表响应（空则 []）
type SealDetectionList = { items: SealSignatureDetection[]; hasMissingRequired: boolean; };
```

**端点**：`GET /api/v1/contracts/{contractId}/seals`
**成功示例**：
```json
{
  "code": "SUCCESS",
  "data": {
    "items": [
      {
        "id": "seal_8821",
        "type": "OFFICIAL_SEAL",
        "status": "VALID",
        "bbox": { "page": 5, "x": 420.0, "y": 680.0, "width": 120.0, "height": 120.0, "coordinateSystem": "PDF_POINTS" },
        "page": 5,
        "extractedText": "浙江网新恒天软件有限公司",
        "isMatchedWithParties": true,
        "complianceNote": "公章清晰，与甲方名称一致",
        "confidence": 0.97
      },
      {
        "id": "seal_8822",
        "type": "HANDWRITTEN_SIGNATURE",
        "status": "SUSPICIOUS_OVERLAP",
        "bbox": { "page": 5, "x": 300.0, "y": 700.0, "width": 90.0, "height": 40.0, "coordinateSystem": "PDF_POINTS" },
        "page": 5,
        "extractedText": null,
        "isMatchedWithParties": false,
        "complianceNote": "签名与印章重叠遮挡，骑缝处不完整，建议补盖清晰骑缝章",
        "confidence": 0.61
      }
    ],
    "hasMissingRequired": false
  },
  "traceId": "tr_77a1",
  "serverTime": "2026-08-13T11:39:04Z"
}
```
**异常示例**：`{ "code": "ERR_OCR_FAILED", "message": "印章区域 OCR 识别失败，请重新上传高清扫描件", "detail": "tesseract confidence < 0.3", "traceId": "tr_77b2" }`

---

### 8.2 跨条款矛盾与逻辑冲突（Cross-Clause Conflict Detection）

**业务价值**：识别同一合同内相互矛盾的条款（付款周期、责任、期限不一致）。

```typescript
type ConflictType =
  | 'PAYMENT_CONTRADICTION'    // 付款矛盾
  | 'LIABILITY_CONTRADICTION'  // 责任矛盾
  | 'TERM_CONTRADICTION'       // 期限矛盾
  | 'OTHER';

interface CrossClauseConflict {
  id: string;
  sourceClauseId: string;       // 冲突源条款 A
  targetClauseId: string;       // 冲突目标条款 B
  conflictType: ConflictType;
  description: string;          // 矛盾描述
  level: RiskLevel;             // 复用风险等级
  suggestion: string;           // 协调统一建议
  citations: PreciseCitation[]; // 见 §8.4 RAG 溯源
}

type ConflictList = { items: CrossClauseConflict[]; };
```

**端点**：`GET /api/v1/contracts/{contractId}/conflicts`
**成功示例**：
```json
{
  "code": "SUCCESS",
  "data": {
    "items": [
      {
        "id": "cf_5521",
        "sourceClauseId": "clause_3_2",
        "targetClauseId": "clause_11_5",
        "conflictType": "PAYMENT_CONTRADICTION",
        "description": "第3.2条约定付款周期为收到发票后30天，第11.5条误写为15天，二者冲突",
        "level": "HIGH",
        "suggestion": "统一付款周期为30天，修改第11.5条",
        "citations": [
          { "sourceType": "STANDARD_CLAUSE", "sourceTitle": "采购标准模板-付款条款", "contentChunk": "买方应在收到合规发票后30日内付款", "relevanceScore": 0.91 }
        ]
      }
    ]
  },
  "traceId": "tr_88c3",
  "serverTime": "2026-08-13T11:40:10Z"
}
```

---

### 8.3 系统级可配置性参数（System Configuration Engine）

**业务价值**：不同业务线合同模板差异大，需支持 Prompt 角色、规则开关、要素抽取的运行时配置。

```typescript
type RiskTolerance = 'STRICT' | 'BALANCED' | 'LOOSE';

interface PromptRoleConfig {
  roleName: string;               // 如 "严苛法务审计员"
  systemPrompt: string;           // LLM System Prompt 模版（支持 {{placeholder}}）
  temperature: number;            // [0.0, 1.0]，Zod range 约束
  riskToleranceLevel: RiskTolerance;
}

interface DynamicExtractionEntityConfig {
  entityKey: string;              // 额外提取项键，如 "guaranteePeriod"
  entityName: string;             // 显示名 "质保期"
  description: string;            // 抽取说明
  isRequired: boolean;            // 是否必填（缺失即报警）
}

interface SystemAuditConfiguration {
  id: string;
  contractType: ContractType;     // 绑定合同类型
  promptConfig: PromptRoleConfig;
  enabledRules: string[];         // 激活的风险规则 ID 列表（空则 []）
  customEntities: DynamicExtractionEntityConfig[]; // 空则 []
  updatedAt: string;              // ISO 8601 UTC（乐观锁）
  version: number;                // 乐观锁
}
```

**端点**：
- `GET /api/v1/settings/rules?contractType=PURCHASE` → 读取配置
- `PUT /api/v1/settings/rules/{configId}` → 更新（需带 `version`，冲突返 `ERR_DATA_CONFLICT`）

**成功示例（GET）**：
```json
{
  "code": "SUCCESS",
  "data": {
    "id": "cfg_purchase_01",
    "contractType": "PURCHASE",
    "promptConfig": {
      "roleName": "严苛法务审计员",
      "systemPrompt": "你是资深法务，对{{contractType}}逐条审查，重点关注付款与违约条款。",
      "temperature": 0.2,
      "riskToleranceLevel": "STRICT"
    },
    "enabledRules": ["R001_LIABILITY", "R002_PENALTY", "R007_JURISDICTION"],
    "customEntities": [
      { "entityKey": "guaranteePeriod", "entityName": "质保期", "description": "保修/质保时长", "isRequired": true }
    ],
    "updatedAt": "2026-08-13T09:00:00Z",
    "version": 7
  },
  "traceId": "tr_99d4",
  "serverTime": "2026-08-13T11:41:00Z"
}
```
**异常示例（PUT 冲突）**：`{ "code": "ERR_DATA_CONFLICT", "message": "配置已被其他用户修改，请重新拉取后再保存", "detail": "version 7 != current 8", "traceId": "tr_99e5" }`

---

### 8.4 思维路径与 RAG 溯源透明化（CoT & Citation Traceability）

**业务价值**：确保可解释性——每条风险与每次重写都给出推理链与法条/标准条款精确溯源。

```typescript
interface ReasoningStep {
  stepNumber: number;             // 从 1 自增
  thought: string;                // 推理步骤描述
  evidenceText: string;           // 引用的原句证据
}

type CitationSourceType = 'STANDARD_CLAUSE' | 'LAW_ARTICLE' | 'PRECEDENT';

interface PreciseCitation {
  sourceType: CitationSourceType;
  sourceTitle: string;            // 如《民法典》第五百八十五条
  contentChunk: string;           // 引用的原文片段
  relevanceScore: number;         // [0,1] 向量匹配度
}

// 扩展后的风险项（继承 §9 RiskItem 并强化）
interface EnhancedRiskItem extends RiskItem {
  reasoningSteps: ReasoningStep[]; // CoT 透明化链条（空则 []）
  citations: PreciseCitation[];    // RAG 精确溯源（空则 []）
}
```

**端点**：`GET /api/v1/contracts/{contractId}/risks`（返回 `EnhancedRiskItem[]`）
**成功示例（片段）**：
```json
{
  "id": "risk_3301",
  "clauseId": "clause_3_2",
  "category": "PENALTY_TOO_HIGH",
  "level": "HIGH",
  "title": "违约金赔偿过高（50%）",
  "originalText": "乙方延迟交货按合同总额50%支付违约金",
  "bbox": { "page": 2, "x": 120.0, "y": 340.0, "width": 300.0, "height": 60.0, "coordinateSystem": "PDF_POINTS" },
  "suggestion": "改为每日0.05%，上限20%",
  "legalBasis": "《民法典》第五百八十五条：约定违约金过分高于损失的，可请求法院适当减少",
  "reasoningSteps": [
    { "stepNumber": 1, "thought": "定位到付款违约金比例为50%", "evidenceText": "按合同总额50%支付违约金" },
    { "stepNumber": 2, "thought": "比对标准模板上限20%，判定过高", "evidenceText": "标准模板：违约金上限不超过合同总额20%" }
  ],
  "citations": [
    { "sourceType": "LAW_ARTICLE", "sourceTitle": "《民法典》第五百八十五条", "contentChunk": "约定的违约金过分高于造成的损失的，人民法院可以适当减少", "relevanceScore": 0.94 },
    { "sourceType": "STANDARD_CLAUSE", "sourceTitle": "采购标准模板-违约条款", "contentChunk": "违约金总额不超过合同金额的20%", "relevanceScore": 0.89 }
  ]
}
```

**AI 重写响应（同样强化溯源）**：
```typescript
interface RewriteResponse {
  taskId: string;
  clauseId: string;
  before: string;
  after: string;
  diff: AuditMessage & { type: 'DIFF' };  // 见 §2.4
  reasoningSteps: ReasoningStep[];
  citations: PreciseCitation[];
  updatedAt: string;
}
```

---

## 9. 核心领域模型全量 TypeScript 类型定义（SSOT 摘要）

```typescript
// 合同主体
interface Contract {
  id: string;
  fileName: string;
  fileKey: string;                // MinIO/OSS key
  fileType: FileType;
  contractType: ContractType;
  typeConfidence: number;         // [0,1]
  status: ContractStatus;
  version: number;                // 当前版本（乐观锁）
  currentVersionId: string;
  elements: ContractElement[];
  risks: EnhancedRiskItem[];
  conflicts: CrossClauseConflict[];
  seals: SealSignatureDetection[];
  missingClauses: MissingClause[];
  healthScore: HealthScore;
  reportId: string | null;
  createdAt: string;
  updatedAt: string;
}

// 关键要素
interface ContractElement {
  key: string;                    // 'partyA'|'partyB'|'signDate'|'amount'|'performancePeriod'|'disputeResolution'|custom
  label: string;
  value: string | null;           // 未抽取为 null
  confidence: number;
  bbox: BBox | null;
  isCustom: boolean;
}

// 风险项（基类，被 §8.4 扩展）
interface RiskItem {
  id: string;
  clauseId: string;
  category: RiskCategory;         // 10+ 类（见下）
  level: RiskLevel;
  title: string;
  originalText: string;
  bbox: BBox;
  suggestion: string;
  legalBasis: string | null;
}

// 风险类别（≥10 类，赛题要求）
type RiskCategory =
  | 'UNLIMITED_LIABILITY'          // 无限责任
  | 'PENALTY_TOO_HIGH'             // 违约金过高
  | 'UNFAVORABLE_JURISDICTION'     // 管辖不利
  | 'UNILATERAL_TERMINATION'       // 单方解约权
  | 'VAGUE_CONFIDENTIALITY'        // 保密义务过宽
  | 'MISSING_DISPUTE_CLAUSE'       // 争议解决缺失
  | 'PAYMENT_TERM_RISK'           // 付款条件不合理
  | 'LIABILITY_IMBALANCE'          // 违约责任不对等
  | 'FORCE_MAJEURE_ABSENT'         // 不可抗力缺失
  | 'IP_OWNERSHIP_RISK'           // 知识产权归属风险
  | 'OTHER';

// 缺失条款
interface MissingClause {
  id: string;
  standardClauseId: string;       // 关联标准模板条款
  title: string;
  reason: string;
  suggestion: string;             // 补全建议
  canAutoFill: boolean;
}

// 健康分（报告雷达图）
interface HealthScore {
  overall: number;                // 0-100
  dimensions: { compliance: number; performanceRisk: number; clauseCompleteness: number };
}

// 反馈标注（数据飞轮）
interface FeedbackItem {
  id: string;
  contractId: string;
  targetType: 'RISK' | 'CONFLICT' | 'SEAL' | 'ELEMENT';
  targetId: string;
  verdict: FeedbackVerdict;
  userCorrection: string | null;  // 用户修正的 Ground Truth
  reviewer: string | null;        // 法务主管审核人
  isIncorporated: boolean;        // 是否已纳入规则库/Prompt
  createdAt: string;
}
```

---

## 10. Agent 交互与分层 Memory 架构数据契约

> 多轮对话修改极易引发上下文超载与状态丢失。强制采用「全局状态 + 局部上下文」分层 Memory。

```typescript
// 全局合同状态（跨会话稳定）
interface GlobalContractState {
  contractId: string;
  meta: { partyA: string; partyB: string; amount: Money; contractType: ContractType };
  astTopology: ClauseNode[];      // 全局 AST 拓扑
  activeVersionId: string;        // 当前激活版本节点
}

// 条款级记忆（局部上下文，仅保留当前条款近 3-5 轮）
interface ScopedClauseMemory {
  targetClauseId: string;
  recentDialog: Array<{ role: 'USER' | 'ASSISTANT'; content: string; createdAt: string }>; // 仅 3-5 轮
  draftAfter: string | null;      // 当前草稿
}

// 动态检索上下文
interface RagRuleContext {
  relatedStandardClauses: Array<{ id: string; title: string; text: string }>;
  relatedLaws: PreciseCitation[];
}

// Agent 重写请求（携带分层上下文，避免全量重载）
interface AgentRewriteRequest {
  contractId: string;
  clauseId: string;
  instruction: string;            // 自然语言指令，如"改成对等解约"
  scopedMemory: ScopedClauseMemory; // 仅传局部
  ragContext?: RagRuleContext;
  xRequestId: string;             // 幂等
}
```

---

## 11. OpenAPI 骨架与生成约定

```yaml
openapi: 3.0.3
info: { title: 合同智能审核系统, version: 1.0.0 }
servers: [ { url: /api/v1 } ]
components:
  securitySchemes:
    bearerAuth: { type: http, scheme: bearer }
  schemas:
    Money:
      type: object
      required: [amount, currency, amountMinorUnit]
      properties:
        amount: { type: string, example: "500000.00" }
        currency: { type: string, enum: [CNY, USD, EUR, OTHER] }
        amountMinorUnit: { type: integer }
    BBox:
      type: object
      required: [page, x, y, width, height, coordinateSystem]
      properties:
        page: { type: integer, minimum: 1 }
        x: { type: number }
        y: { type: number }
        width: { type: number }
        height: { type: number }
        coordinateSystem: { type: string, enum: [PDF_POINTS] }
    # ... SealSignatureDetection / CrossClauseConflict / SystemAuditConfiguration
    #     / EnhancedRiskItem / ReasoningStep / PreciseCitation 均在此声明
```

> **生成命令（CLI）**：`npm run gen:contract` 从 `openapi.yaml` 同步产出 `types.ts`、`*.py`（pydantic）、`*.zod.ts`。任何一端修改须经此链路回写，禁止旁路。

---

## 12. 自检清单（交付前核对）

- [ ] 所有响应集合字段空时为 `[]` 非 `null`
- [ ] 所有 ID / Snowflake 为 `string`
- [ ] 金额 `Money`、百分比 `[0,1]`、时间 ISO8601 UTC
- [ ] 多态数据含 `type` 鉴别器
- [ ] 业务错误用 `code` 字符串枚举，HTTP 仅表系统层
- [ ] 写操作支持 `X-Request-ID` 与 `version` 乐观锁
- [ ] 文件上传走 presign 三步法
- [ ] 长耗时任务返回 `taskId` + 进度订阅
- [ ] 批量操作返回 `successIds`/`failedItems`
- [ ] 四大缺失维度（印章/跨条款/可配置/CoT）已补齐类型+端点+示例
- [ ] 所有类型由 Schema 生成，UI 兜底显式约定
