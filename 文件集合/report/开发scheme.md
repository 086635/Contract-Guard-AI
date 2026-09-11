<br />

***

### 一、 Infra 必须给前端提供什么（“零门槛”交付包）

除了完整的 API 接口外，为了实现“无缝合并”且不被改乱，你需要向前端交付以下 **4 项基础设施与文档**：

| 交付物                     | 具体内容                                                          | 作用                                        |
| ----------------------- | ------------------------------------------------------------- | ----------------------------------------- |
| **1. 可运行的 Stub 服务**     | 19 个端点取消 501，全部绑定 `response_model` 并返回结构化 Mock 数据             | 前端本地启动 FastAPI 后，发请求能直接拿到真实格式的响应数据        |
| **2. 强类型 SDK / Zod 校验** | 运行 `pnpm gen` 生成最新的 `types.ts` 和 `zod.ts`                     | 前端写代码时，所有 API 参数、返回值均有 TypeScript 自动补全和校验 |
| **3. 前端开发规范与路由文档**      | 《前端开发指导契约》（包含页面路由表、状态机扭转表、接口映射）                               | 明确告知前端“哪个页面调用哪个 API”、“字段语义是什么”            |
| **4. 隔离的开发工作区**         | 限定开发范围：\*\*严格仅限 `apps/web-ui/src/{views,components,routes}**` | 物理隔离，确保前端代码无论怎么改，都不可能破坏后端与 Infra          |

***

### 二、 实现“零摩擦并行与一键无缝合并”的具体操作流程

1. **第一步：后端打通 Stub（实现 Infra 100% 就绪）:**
   无需实现真实 DB/AI 逻辑，仅需把 19 个路由的 `501` 替换为响应 Mock 数据的 Stub：

```python
# apps/api-gateway/app/api/v1/endpoints/contracts.py
@router.get("/{contract_id}", response_model=ContractDetailResponse)
async def get_contract_detail(contract_id: str):
    # 直接返回符合 SSOT 契约的 Mock DTO
    return ContractDetailResponse(
        id=contract_id,
        filename="采购合同示例.pdf",
        status=ProcessingStatus.SUCCESS,
        risk_summary={"high": 2, "medium": 1, "low": 0}
    )

```

同时运行 `pnpm --filter @contract-guard/schema gen`，更新前端类型包。

1. **第二步：书写《前端开发接入规范》文档:**
   在项目根目录新建 `docs/FRONTEND_GUIDE.md`，明确告知：
2. **环境启动**：只需安装 Node.js，拉取代码后运行 `pnpm install`，再执行 `pnpm --filter @contract-guard/web-ui dev` 启动。
3. **API 调用方式**：强制使用 `src/api/generated/` 下自动生成的 Axios/Fetch Client 和 Types，禁止手写 API 类型。
4. **页面与路由映射表**：

- `/contracts` -> 合同列表页（调用 `GET /api/v1/contracts`）
- `/contracts/:id` -> 合同详情与风险面板（调用 `GET /api/v1/contracts/{id}`）

1. **第三步：降低 Git 门槛（新手友好防错）:**
   针对前端同学不熟悉 Git 的情况，采用以下方案：
2. **使用 VS Code 内置图形化 Git / GitHub Desktop**：教她简单的三步可视化操作：`Fetch` -> `Commit` -> `Push`。
3. **创建专属功能分支**：在你的电脑上新建分支并推送到远程：

```bash
git checkout -b feature/web-ui-views
git push -u origin feature/web-ui-views

```

1. **制定修改禁区**：约定她**只能提交** **`apps/web-ui/src/`** **内的改动**，绝不触碰 `packages/` 与 `apps/api-gateway/`。
2. **第四步：并行开发与最终一键合并:**

- **前端同学**：在 `feature/web-ui-views` 分支上，对着本地运行的 FastAPI Stub 服务写页面组件、搭 UI 界面。
- **你**：在 `main` 或 `feature/ai-core` 分支上，专注编写 LangGraph / RAG / OCR / DB 持久化，替换后端 Stub。
- **合并时机**：你只需通过 Git 将 `feature/web-ui-views` 分支合并到主干（`git merge feature/web-ui-views`）。由于前端调用的是同一套 DTO 类型，只要你后端将真实 DB/AI 数据套入同样的 DTO 返回，**前端页面无需修改一行代码即可瞬间接入真实业务**。

***

### 三、 给前端同学的《接入规范文档》模板提纲

建议你按照以下结构整理 `docs/FRONTEND_GUIDE.md` 交付给她：

- **环境配置与命令**：Node 20+ 环境准备、`pnpm dev` 启动前端、后端 Stub 启动命令。
- **修改范围约束**：可修改目录（`apps/web-ui/src/`）、禁止修改目录（`packages/`、`apps/api-gateway/`）。
- **接口与数据结构约定**：直接引用 `src/api/generated/types.ts`，说明各页面所需的 Data Mock 含义。
- **全局 UI 与状态规范**：路由配置路径、Component 拆分原则、Antd / Tailwind 视觉统一样式。

