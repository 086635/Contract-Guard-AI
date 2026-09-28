# 4-3 · Worker 与报告收口（T-D3）

> 所属模块：4 · 交付　归属任务：**T-D3**　公共规范见 `0.施工标准与定义归属.md`
> 模块总纲：`4.交付模块.md` §4.5/§4.6　规划文档：`项目路径规划2.0.md` §4 M3
> 是否触碰契约层：❌ 否（报告元数据用 openapi `ReportData`）
> 落点：`apps/async-worker/worker/tasks/gen_report.py`、`worker/storage/minio_client.py`、`worker/report/`、`apps/api-gateway/app/services/report_service.py`
>
> ✅ **组件拆解已落盘**：执行切面为 `4.C18` ~ `4.C21`；
> 详细依赖与状态见 `4.交付模块.md` §4.5.1。
>
> ⚠️ **实施纠偏（2026-09-27）**：worker 不直连 PostgreSQL。
> `audit_reports` 由网关在 `GET /report/{contractId}` 根据 Celery 任务结果落库，见 `4.C21`。

## ① 元信息

| 项 | 值 |
|---|---|
| 任务 ID | **T-D3**（Worker 与报告收口） |
| 优先级 | **P0**（报告导出是赛题硬指标） |
| 人日 | 1.5~2 |
| 前置依赖 | T-D1（`audit_reports`/`audit_risks` 表就绪） |
| 影响面 | worker 报告任务 + MinIO 上传 + 网关报告元数据落库；不改审核逻辑 |

## ② 范围

### ✅ 做

- `gen_report` Celery 任务：消费 C21 组装的 `audit_risks`/要素/溯源载荷 → python-docx 生成 DOCX
- 实现 `minio_client.upload_report`：传 `reports/` 桶，返回 object key
- 网关按 Celery 任务结果写 `audit_reports` 元数据（`format=DOCX`、`storage_path`、`status`）
- 下载：`GET /report/{contractId}` 运行时生成 presigned `downloadUrl`

### ❌ 不做

- ❌ 不改 `EnhancedRiskItem`/`CrossClauseConflict` 字段
- ❌ 不做 PDF（首版只 DOCX）
- ❌ 不在 worker 重写风险/矛盾内容（只读已有结果）

## ③ 定义归属

| 本卡**定义**（唯一处） | 本卡**引用**（不重复定义） |
|---|---|
| DOCX 报告模板与渲染函数（`worker/report/`） | openapi `ReportData` |
| `upload_report` 实现 | `audit_risks`/`contract_elements`（T-D1） |
| `gen_report` 任务载荷键名（本节 §⑤.1） | `minio_client.get_minio_client`（已存在） |
| 网关结果落库逻辑（`4.C21`） | `ReportRepository.create/get`（T-D1） |

> 🔴 **适配性三问**：① 依赖方向：worker 可依赖左侧与 `ai_engine` 的领域模型。② 复用唯一处符号：`ReportData`、`EnhancedRiskItem`、`get_minio_client`。③ 不许新造：不另写风险字段、不另写 MinIO client。

## ④ 交付物

| 文件 | 动作 | 职责 |
|---|---|---|
| `worker/tasks/gen_report.py` | 新建/补全 | `gen_report` 任务体 |
| `worker/report/render_docx.py` | 新建 | DOCX 渲染（风险/建议/溯源） |
| `worker/storage/minio_client.py` | 修改 | 实现 `upload_report` |
| `worker/progress.py` | 修改 | `REPORT_STAGES` 与 `ReportProgressReporter` |
| `apps/api-gateway/app/services/report_service.py` | 修改 | 组装任务载荷、按任务结果落库 |
| `apps/async-worker/tests/test_gen_report.py` | 新建 | 离线渲染单测 |
| `apps/api-gateway/app/api/v1/tests/test_report_real.py` | 修改 | DONE/RUNNING/FAILED 快照集成测试 |

## ⑤ 接口规格

```python
# worker/tasks/gen_report.py
@celery_app.task(name="gen_report", bind=True)
def gen_report_task(
    self,
    contract_id: str,
    payload: dict[str, Any] | None = None,
) -> dict:
    """渲染 DOCX、上传 MinIO，返回供网关落库的结构化结果。"""

# worker/storage/minio_client.py
def upload_report(contract_id: str, data: bytes, content_type: str) -> str:
    """上传到 reports/ 桶，返回 object key。"""
```

### ⑤.1 `gen_report` 载荷契约

| 键 | 类型 | 必填 | 来源 |
|---|---|---:|---|
| `contractId` | `string` | 是 | `contract_id` |
| `generatedAt` | ISO-8601 `string` | 是 | 网关组装时间 |
| `risks` | `list[EnhancedRiskItem]` JSON | 是 | `audit_risks` |
| `conflicts` | `list[CrossClauseConflict]` JSON | 是 | `audit_risks` |
| `elements` | `list[ContractElement]` JSON | 是 | `contract_elements` |
| `healthScore` | `HealthScore` JSON | 否 | 当前无落点，先不传 |

## ⑥ 实现规范

1. DOCX 内容必须含：风险列表（等级/类型/描述/建议）、矛盾对、逐条溯源 `citations`。
2. `upload_report` 用 `put_object`，key 形如 `reports/{contract_id}/{timestamp}.docx`。
3. worker 生成并上传后只返回 `storagePath`；网关在 `GET` 时写 `audit_reports` 并置 `status=DONE`；失败不落报告行。
4. `downloadUrl` 只在 `GET` 时 presign，**不落库**。

## ⑦ 验收标准（**可执行断言**）

| # | 断言 |
|---|---|
| 1 | `gen_report_task` 在离线夹具下产出可打开的 DOCX（用 `python-docx` 回读校验段落） |
| 2 | `upload_report` 上传后 `fget_object` 可读回等值字节 |
| 3 | 任务 `DONE` 后，`GET /report/{contractId}` 写 `audit_reports`：`status=DONE`、`format=DOCX`、`storage_path` 非空 |
| 4 | 未设 MinIO 环境变量时单测 skip，不 fail/error |
| 5 | 全量 pytest 不低于当前基线 **597 passed / 10 skipped** |

### 实测后回填

| 项 | 实测值 |
|---|---|
| DOCX 生成 | ⬜ 待填 |
| MinIO 上传 | ⬜ 待填 |
| 全量 pytest | ⬜ 待填 |

## ⑧ 测试要求

离线渲染单测（不依赖 MinIO）：构造固定 `audit_risks` 夹具 → 渲染 → `Document` 回读断言标题/风险条数；MinIO 用例用 `skipif`。

## ⑨ 避坑清单

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | 报告内容漏溯源 | 每条风险带 `citations` |
| 2 | presign URL 落库 | 运行时生成 |
| 3 | DOCX 用模板字符串拼 XML | 用 python-docx 对象 API |
| 4 | 未设 MinIO 时 fail | 单测 skip |

## ⑩ 可继续拆分的子单元（Plan 输入）

| 组件 | 执行卡 | 判据 |
|---|---|---|
| DOCX 渲染 | `4.C18` | 回读段落与溯源正确 |
| MinIO 上传 | `4.C19` | 往返字节一致 |
| gen_report 编排 | `4.C20` | DONE/FAILED 结果与上传 key |
| 元数据落库 | `4.C21` | `GET` 按任务结果写 `audit_reports` |

### ⑩.1 T-D3 组件顺序

```text
C18 ─┐
     ├─> C20 ──> C21
C19 ─┘
```
