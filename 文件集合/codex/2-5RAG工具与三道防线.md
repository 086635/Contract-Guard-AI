# 2-5 · RAG 工具与三道防线（T-B4）

> 所属模块：2 · Agent　公共规范见 `0.施工标准与定义归属.md`（下称"标准"）
> 模块总纲：`2.AGENT模块.md` §2.4.1（`standard_compare` 用空召回作缺失信号）/ §2.8（**禁止重写检索**）

## ① 元信息

| 项 | 值 |
|---|---|
| 任务 ID | **T-B4** |
| 优先级 | **P0**（赛题"报告含法律依据"指标的唯一来源） |
| 人日 | 1.5 |
| 前置依赖 | **T-B3**（六节点里 `risk` / `standard_compare` 要调它）；🔴 **跨模块前置：T-A7**（RAG 的 8 项失效配置接线） |
| 影响面 | `packages/ai-engine/ai_engine/agents/tools.py`（新建）、`ai_engine/rag/**`（改造为薄桥接）、`packages/ai-engine/tests/` |
| 是否触碰契约层 | ❌ 否（`AgentRAGToolInput` / `AgentRAGToolResult` **已存在且冻结**） |

> 🔴 **跨模块依赖的原因**：三道防线里的 ①超长拦截与 ③超时重试必须消费** RAG 侧配置**
> （`max_query_chars` / `timeout_ms` / `max_retries`）。若 T-A7 未完成、这些配置仍无人读，
> 本任务**只能硬编码常量** —— 那会重造"声明了没人消费"的历史缺陷。

---

## ② 范围

### ✅ 做

- 新建 `ai_engine/agents/tools.py`：把 `RAGService` 包装成**唯一**的 RAG 工具入口
- **三道防线落地**：①超长拦截（头+尾截断 + warning）②空召回降级（映射为 `EMPTY_CONTEXT_HINT`）③超时重试（指数退避）
- 导出 `build_function_calling_spec()`（**复用**，不重写）供 LLM function-calling 使用
- 提供 `as_langchain_tool()`（`@tool` 包装），供 LangGraph 节点/ReAct 兼容调用
- **消除 L3 F6**：把 `ai_engine/rag/*` 的两套并行检索实现改为薄桥接（删除 `retriever.py` / `reranker.py` 的 `NotImplementedError` 骨架）

### ❌ 不做

- ❌ **不重新实现检索/融合/重排**（`service.py` 已有；在 `ai_engine/rag/*` 里再写一套就是 F6 的重演）
- ❌ 不改 `RAGService` / `rag_contracts`（端口-适配器红线，标准 §0.3）
- ❌ 不另写 `EMPTY_CONTEXT_HINT` / 工具名 / JSON Schema（**唯一处**：`rag_contracts/agent_tool.py`）
- ❌ 不直接 import `qdrant_client` / `pymilvus` / `httpx`（SDK 泄漏）
- ❌ 不在工具里读 `os.getenv`（配置一律经 `RagSettings`，标准 §0.4）

---

## ③ 定义归属

| 本文件**定义**（唯一处） | 本文件**引用**（不重复定义） |
|---|---|
| `RagTool` 类与 `build_rag_tool()`（`agents/tools.py`） | `TOOL_NAME` / `TOOL_DESCRIPTION` / `EMPTY_CONTEXT_HINT` / `AgentRAGToolInput` / `AgentRAGToolResult` / `build_function_calling_spec`（`rag_contracts/agent_tool.py`） |
| **`truncate_query()` 截断算法**（头+尾 + 省略标记） | `RAGService` / `RAGService.retrieve`（`rag_core/service.py`） |
| **重试策略表**（哪些 `fallback_reason` 才重试） | `RAGQueryRequest` / `RAGQueryResponse` / `CitationOut` / `FallbackReason`（`rag_contracts/query.py`） |
| — | `RagSettings.max_query_chars / timeout_ms / max_retries / score_threshold`（`rag_core/config.py`） |
| — | `build_service_from_settings`（`rag_core/factory.py`，离线默认 `inmemory`） |

---

## ④ 交付物

| 文件 | 动作 | 职责 |
|---|---|---|
| `packages/ai-engine/ai_engine/agents/tools.py` | **新建** | `RagTool` / `build_rag_tool` / `truncate_query` / 重试与降级 |
| `packages/ai-engine/ai_engine/agents/__init__.py` | 修改 | 补导出 `RagTool` / `build_rag_tool` |
| `packages/ai-engine/ai_engine/rag/__init__.py` | **改造** | 薄桥接：re-export `RagTool` / `build_rag_tool` |
| `packages/ai-engine/ai_engine/rag/retriever.py` | **删除** | 旧骨架（`HybridRetriever` → 由 `RagTool` 取代），消除 F6 |
| `packages/ai-engine/ai_engine/rag/reranker.py` | **删除** | 旧骨架（`Reranker` → 精排由 RAG 侧 `RerankerPort` 承担） |
| `packages/ai-engine/tests/test_rag_tool.py` | **新建** | 三道防线用例（§⑧） |

---

## ⑤ 接口规格

```python
# ai_engine/agents/tools.py
@dataclass(frozen=True, slots=True)
class TruncationResult:
    """截断结果。"""

    query: str          # 截断后的查询（未超限时与入参逐字相同）
    truncated: bool
    omitted_chars: int  # 被省略的字符数（0 表示未截断）


def truncate_query(query: str, max_chars: int) -> TruncationResult:
    """超长查询拦截（**防线 ①**）。

    算法（唯一处）：
      1. len(query) <= max_chars → 原样返回；
      2. 否则取 tail_budget = min(200, max_chars // 5)，head_budget = max_chars - tail_budget - len(marker)；
      3. 拼接 head + marker + tail，其中 marker = f"……（中间省略 {omitted} 字符）……"；
      4. 保证 head_budget >= 1；结果长度**严格 <= max_chars**。
    """
```

```python
class RagTool:
    """RAG 工具（三道防线在此落地）。

    设计要点：
    1. 只做**包装**：检索逻辑全部在 `RAGService`，本类不碰向量库、不做融合。
    2. 契约对齐：入参/出参与 `rag_contracts.agent_tool` 的冻结模型逐字段一致。
    3. 永不抛异常：失败一律转成 `status="EMPTY"`（让 LLM 知道"没有依据"）。
    """

    def __init__(
        self,
        service: RAGService | None = None,
        settings: RagSettings | None = None,
        *,
        sleeper: Callable[[float], None] = time.sleep,   # 测试注入，避免真实等待
    ) -> None: ...

    def __call__(
        self,
        query: str,
        *,
        contract_type: ContractType | None = None,
        source_types: list[SourceType] | None = None,
        top_k: int = 5,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> AgentRAGToolResult: ...

    def as_langchain_tool(self) -> BaseTool:
        """包装为 LangChain Tool（name == TOOL_NAME）。"""

    @property
    def spec(self) -> dict[str, Any]:
        """function-calling 描述（直接复用 rag_contracts 的生成器）。"""


def build_rag_tool(
    service: RAGService | None = None, settings: RagSettings | None = None
) -> RagTool:
    """构造 RAG 工具；service 为 None 时按 RagSettings 走 factory（默认 inmemory，离线可用）。"""
```

### 三道防线的判定表（**唯一处**）

| 防线 | 触发条件 | 动作 | status | 记录 |
|---|---|---|---|---|
| ① 超长拦截 | `len(query) > max_chars`（`max_chars = min(settings.max_query_chars, 2000)`） | 头+尾截断 | 不影响（正常检索） | `warnings += ["OVERLONG_QUERY: 已截断 N 字符"]` + `log_event("rag_tool.truncate")` |
| ② 空召回降级 | `not resp.retrieved_docs`（或 service 已判 `EMPTY`） | `context_block = EMPTY_CONTEXT_HINT`、`citations=[]` | `EMPTY` | `fallback_reason` 取自 `resp` |
| ② 部分降级 | `resp.degraded is True` **且** `retrieved_docs` 非空 | 正常返回上下文 | `DEGRADED` | 透传 `resp.warnings` |
| ③ 超时重试 | 抛异常，或 `fallback_reason ∈ {TIMEOUT, STORE_UNAVAILABLE}` | 指数退避重试至 `max_retries` 次 | 成功→`OK`；耗尽→`EMPTY` | `log_event("rag_tool.retry", attempt=k)` |
| ③ 不重试 | `fallback_reason == EMPTY_RECALL` | **立即返回**（重试无意义且放大延迟） | `EMPTY` | — |

> 🔴 **契约硬上限 2000 与配置上限取 `min`**：`AgentRAGToolInput.query` 的 `max_length=2000` 是**冻结契约**，
> 配置若被调大到 3000，工具侧必须仍按 2000 截断，否则工具定义与实际入参不一致（LLM 入参校验会失败）。

---

## ⑥ 实现规范

1. 🔴 **单一入口**：全仓调用检索的路径只有 `RagTool.__call__`；`ai_engine` 内**不得**出现第二处 `RAGService(...)` 构造（工厂除外）。
2. 🔴 **异常必须记日志**：`except` 分支 `log_event(_LOG, "rag_tool.degrade", level="WARNING", trace_id=..., reason=...)`（标准 §0.5.3）。
3. 🔴 **`context_block` 只允许两种来源**：`resp.context_block`（有依据）或 `EMPTY_CONTEXT_HINT`（无依据）。**禁止**自行拼接话术。
4. 🔴 **`citations` 直接取 `[d.citation for d in resp.retrieved_docs]`**，字段与 `EnhancedRiskItem.citations` 1:1（`rag_contracts/query.py` 已保证）。
5. 🔴 **`token_usage` 透传** `resp.token_usage`（含 `total_tokens`），`took_ms` 取工具自身墙钟耗时。
6. 🔴 **退避参数固定**：`base=0.2s`、`factor=2`、`cap=2.0s`，总预算 `<= timeout_ms/1000 * (1 + max_retries)` 秒；**必须**通过注入的 `sleeper` 等待（测试零等待）。
7. 🔴 **配置只从 `RagSettings` 读**：`RagSettings()` 的 `CG_RAG_*` 前缀即配置来源（标准 §0.4）；工具内**不得** `os.getenv`。
8. ⚠️ `top_k` 夹取到 `[1, 20]`（与 `AgentRAGToolInput.top_k` 的 `le=20` 一致），超出记 warning。
9. ⚠️ `reason` 只进日志（`log_event(..., reason=...)`），**不影响**检索参数。
10. ⚠️ 拆除 `ai_engine/rag/{retriever,reranker}.py` 时，**同一批**更新唯一消费者 `worker/tasks/run_audit.py`（属 `2-6`）；若 2-6 尚未开工，先在 `rag/__init__.py` 保留同名**弃用别名**并在文档回填中登记。

### 实测后回填（DoD 要求）

| 项 | 实测值（落地时填写） |
|---|---|
| 截断阈值实际生效值（`CG_RAG_MAX_QUERY_CHARS`） | ⬜ 待回填 |
| 单次检索平均耗时 / P95 | ⬜ 待回填 |
| 重试触发次数（离线桩/真实服务各一组） | ⬜ 待回填 |
| 工具调用占 LLM 上下文 Token 比例 | ⬜ 待回填 |

---

## ⑦ 验收标准（**逐条可执行**）

> 环境前缀见 `AGENTS.md` §4.1。全部断言**离线可跑**（默认 `inmemory` 向量库 + 桩 service）。

**断言 1 · 工具定义与契约一致**

```powershell
D:\aanacoda1\python.exe -c "from ai_engine.agents.tools import build_rag_tool; s=build_rag_tool().spec; f=s['function']; assert f['name']=='retrieve_contract_knowledge' and f['strict'] is True; p=f['parameters']; assert p['additionalProperties'] is False and set(p['required'])==set(p['properties']); assert '$defs' not in p; print('spec ok')"
```

**断言 2 · 超长截断且不超上限**

```powershell
D:\aanacoda1\python.exe -c "from ai_engine.agents.tools import truncate_query; r=truncate_query('甲'*5000, 2000); print(r.truncated, r.omitted_chars, len(r.query)); assert r.truncated and len(r.query)<=2000 and r.omitted_chars>0"
```

**断言 3 · 空召回 → `EMPTY` + 固定提示语**

用桩 `RAGService` 返回空响应 → 断言 `status == "EMPTY"`、`context_block == EMPTY_CONTEXT_HINT`（**字符串相等**）、`citations == []`。

**断言 4 · 异常重试后成功**

桩 service 前 2 次抛 `RuntimeError`、第 3 次返回正常响应，`max_retries=2` →
断言 `调用次数 == 3`、`status == "OK"`、**无异常抛出**、`sleeper` 收到 2 次调用且等待时长按 0.2/0.4 递增。

**断言 5 · 重试耗尽 → `EMPTY` 且仍是合法契约对象**

**断言 6 · `EMPTY_RECALL` **不**重试**

桩 service 返回 `degraded=True, fallback_reason=EMPTY_RECALL` → 断言 `调用次数 == 1`。

**断言 7 · LangChain 工具可注册**

```powershell
D:\aanacoda1\python.exe -c "from ai_engine.agents.tools import build_rag_tool; t=build_rag_tool().as_langchain_tool(); assert t.name=='retrieve_contract_knowledge'; print('langchain tool ok')"
```

**断言 8 · 无 SDK 泄漏 / 无第二套检索**

```powershell
D:\aanacoda1\python.exe -c "import io; t=io.open('packages/ai-engine/ai_engine/agents/tools.py',encoding='utf-8').read(); bad=[k for k in ['qdrant_client','pymilvus','httpx','import requests'] if k in t]; print(bad); assert not bad"
```

**断言 9 · 测试基线**：`D:\aanacoda1\python.exe -m pytest -q` → **≥ 140 passed**。

---

## ⑧ 测试要求

`packages/ai-engine/tests/test_rag_tool.py`（全部离线，桩 service，不联网、不需要 Qdrant）：

| 用例 | 断言 |
|---|---|
| `test_spec_is_strict_and_self_contained` | §⑦ 断言 1 |
| `test_truncate_query_head_tail` | §⑦ 断言 2；另测 `len == max_chars` 边界（不截断）与 `max_chars < len(marker)` 的极端参数 |
| `test_empty_recall_maps_to_hint` | §⑦ 断言 3（并断言**未**新建提示语字符串：`EMPTY_CONTEXT_HINT in result.context_block`） |
| `test_retry_then_success` | §⑦ 断言 4（用注入 `sleeper` 记录等待序列） |
| `test_retry_exhausted_returns_empty` | §⑦ 断言 5 |
| `test_empty_recall_not_retried` | §⑦ 断言 6 |
| `test_top_k_clamped_with_warning` | `top_k=99` → 实际 20 且 warnings 非空 |
| `test_degraded_with_docs_returns_degraded_status` | 部分降级映射到 `DEGRADED` |
| `test_as_langchain_tool_name` | §⑦ 断言 7 |
| `test_no_sdk_leak_in_source` | §⑦ 断言 8 |

> 🔴 需要真实 Qdrant 的用例一律 `skipif`（`CG_RAG_TEST_QDRANT_URL`，标准 §0.6.2）。

---

## ⑨ 避坑清单

| # | 坑 | 说明 |
|---|---|---|
| 1 | 在 `ai_engine/rag/*` 里"顺手"实现检索 | 这就是 L3 **F6**。本任务把该目录降级为薄桥接 |
| 2 | 另写一份 `EMPTY_CONTEXT_HINT` | 形成第二处定义；`risk` 节点的缺失判定会与 RAG 侧漂移 |
| 3 | 直接 `import httpx` / `qdrant_client` 调服务 | SDK 泄漏；换向量库要改 Agent 代码（架构优势归零） |
| 4 | 对 `EMPTY_RECALL` 做重试 | 空召回是**握手信号**不是故障；重试只会放大延迟 |
| 5 | 截断后把 query 回写 state / 日志里打全文 | 泄露无关内容、污染可观测；只记 `omitted_chars` |
| 6 | 用真实 `time.sleep` 跑测试 | 单测会白白慢数秒；注入 `sleeper` |
| 7 | 忘了取 `min(max_query_chars, 2000)` | 配置 > 2000 时，工具定义与实际入参不一致 → LLM 调用校验失败 |
| 8 | 把 `trace_id` 丢了 | 埋点断链，跨模块（worker → graph → tool → rag_core）无法串联 |
| 9 | 认为"T-A7 做完再做也不迟" | 关键路径上它**是前置**：不做则防线①③只能硬编码常量 |