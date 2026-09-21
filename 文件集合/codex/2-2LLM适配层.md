# 2-2 · LLM 适配层（T-B1）

> 所属模块：2 · Agent　公共规范见 `0.施工标准与定义归属.md`（下称"标准"）
> 模块总纲：`2.AGENT模块.md` §2.1.2（关键词"单一入口"）/ §2.2（现状：**全包无模型构造入口**）

## ① 元信息

| 项 | 值 |
|---|---|
| 任务 ID | **T-B1** |
| 优先级 | **P0**（关键路径第二环：2-3 / 2-4 / 2-5 全部依赖"拿到一个模型"） |
| 人日 | 0.5 |
| 前置依赖 | **T-B0**（依赖与版本收口）、决策 **D2-a**（接入方式）、决策 **D2-d**（供应商与模型名） |
| 影响面 | `packages/ai-engine/ai_engine/llm/**`（新建）、`packages/ai-engine/ai_engine/__init__.py`（仅补导出） |
| 是否触碰契约层 | ❌ 否（`LlmSettings` 是**本包私有配置**，不进 `rag_contracts`） |

---

## ② 范围

### ✅ 做

- 新建 `ai_engine/llm/` **唯一模型构造点**（`settings.py` / `factory.py` / `structured.py` / `errors.py` / `offline.py`）
- 定义 `LlmSettings`（`CG_LLM_*` 前缀，默认值**离线可跑**）
- 提供 `build_chat_model()` / `get_chat_model(role)` / `clear_llm_cache()`
- 提供 `invoke_structured()`：**结构化输出 + 失败降级**（返回 `(None, warnings)` 而非抛异常）
- 提供离线桩模型：**无需 API Key 也能产出符合 schema 的最小 JSON**，让 `audit_graph` 离线端到端可跑

### ❌ 不做

- ❌ 不写任何业务 Prompt 模板（属 **2-9**，本文件只提供"怎么调模型"）
- ❌ 不建图、不写节点（属 **2-3 / 2-4**）
- ❌ 不在 `ai_engine/rag/*` 里构造模型（该目录将被 `2-5` 改造为薄桥接）
- ❌ 不引入 `langchain-openai` 之外的 `langchain-*` 集成包；❌ 不引入 `torch` / `transformers`
- ❌ 不把 API Key 写进代码 / 测试 / 文档（只读环境变量）

---

## ③ 定义归属

| 本文件**定义**（唯一处） | 本文件**引用**（不重复定义） |
|---|---|
| `LlmSettings`（`ai_engine/llm/settings.py`）——**全仓唯一** | 配置前缀规则 `CG_LLM_`（标准 §0.4） |
| `build_chat_model` / `get_chat_model` / `clear_llm_cache`（`llm/factory.py`） | `PromptLibrary` / `PromptTemplate`（`ai_engine/prompts/loader.py`，**已存在，不改**） |
| `invoke_structured` 及其降级语义（`llm/structured.py`） | 日志与埋点 API `get_logger` / `log_event`（`cg_observability`） |
| `LlmConfigError`（`llm/errors.py`） | 工具定义 `build_function_calling_spec()`（`rag_contracts/agent_tool.py`，属 **2-5** 消费） |
| 离线桩的确定性产出算法（`llm/offline.py`） | 节点降级写法「返回 warnings 不抛异常」（标准 §0.5.2） |

> 🔴 **`LlmConfigError` 继承 `rag_contracts.errors.RagError`**（标准 §0.5.1：全仓异常体系收敛到一处），
> `retryable=False`、**沿用基类默认错误码**（不新增 `CG-RAG-xxxx` 常量、不改契约层）。
> 它只在**构建期**（`build_chat_model`）由配置错误触发，**快速失败**优于降级；
> 运行期（节点内）一律**不抛异常**，走 `invoke_structured` 的 `(None, warnings)` 降级路径。

---

## ④ 交付物

| 文件 | 动作 | 职责 |
|---|---|---|
| `packages/ai-engine/ai_engine/llm/__init__.py` | **新建** | 导出 5 个公开符号 + `__all__`（字母序） |
| `packages/ai-engine/ai_engine/llm/settings.py` | **新建** | `LlmSettings`（`BaseSettings`，`env_prefix="CG_LLM_"`） |
| `packages/ai-engine/ai_engine/llm/factory.py` | **新建** | `build_chat_model` / `get_chat_model` / `clear_llm_cache`（进程内缓存） |
| `packages/ai-engine/ai_engine/llm/structured.py` | **新建** | `invoke_structured`（结构化输出 + 降级） |
| `packages/ai-engine/ai_engine/llm/offline.py` | **新建** | 离线桩 `build_offline_model()` + schema→最小 JSON 算法 |
| `packages/ai-engine/ai_engine/llm/errors.py` | **新建** | `LlmConfigError` |
| `packages/ai-engine/tests/test_llm_factory.py` | **新建** | 工厂 / 缓存 / 降级 / 离线桩用例 |
| `packages/ai-engine/ai_engine/__init__.py` | 修改 | `__version__` 旁补一行说明（**不 re-export** `llm`，避免包级副作用） |

---

## ⑤ 接口规格

```python
# ai_engine/llm/settings.py
class LlmSettings(BaseSettings):
    """LLM 接入配置（唯一处）。

    默认值必须离线可跑：provider 默认 "offline"，不读 API Key、不联网。
    """

    model_config = SettingsConfigDict(
        env_prefix="CG_LLM_", env_file=".env", extra="ignore"
    )

    provider: Literal["offline", "openai"] = "offline"
    model: str = "gpt-4o-mini"          # provider="openai" 时生效
    temperature: float = 0.0            # 审核/重写要求可复现 → 默认 0
    max_tokens: int = 2048
    timeout_s: float = 30.0
    max_retries: int = 2
    json_mode: Literal["json_schema", "json_mode", "none"] = "json_schema"
    api_key: str | None = None          # 禁止写死，只从环境变量读
    base_url: str | None = None
```

```python
# ai_engine/llm/errors.py
class LlmConfigError(RagError):
    """LLM 构建期配置错误（缺 API Key / provider 非法）。

    retryable=False：配置错误重试无意义，必须改配置。
    """

    retryable = False
```

```python
# ai_engine/llm/factory.py
def build_chat_model(
    settings: LlmSettings | None = None, *, temperature: float | None = None
) -> BaseChatModel:
    """构造一个 ChatModel（唯一构造点）。

    Args:
        settings: 配置；None 时取 LlmSettings()。
        temperature: 覆盖配置里的温度（**角色级差异用**，如 classify 用 0）。

    Returns:
        LangChain BaseChatModel 实例。

    Raises:
        LlmConfigError: provider="openai" 但缺少 api_key。
    """

def get_chat_model(role: str = "default", settings: LlmSettings | None = None) -> BaseChatModel:
    """按角色取模型（进程内缓存，避免每节点重建）。"""

def clear_llm_cache() -> None:
    """清空进程内模型缓存（测试与配置热更新用）。"""
```

```python
# ai_engine/llm/structured.py
def invoke_structured(
    model: BaseChatModel,
    schema: type[_TModel],
    messages: list[BaseMessage],
    *,
    fallback: _TModel | None = None,
) -> tuple[_TModel | None, list[str]]:
    """调用模型并解析为 schema 实例（**永不抛异常**）。

    Returns:
        `(value, warnings)`。成功时 `warnings == []`；
        失败时 `value is None`（或传入的 `fallback`）且 `warnings` 非空。
    """
```

```python
# ai_engine/llm/offline.py
def build_offline_model() -> BaseChatModel:
    """离线桩：对任意 `with_structured_output(schema)` 返回**符合 schema 的最小 JSON**。"""

def minimal_json_for(schema: type[BaseModel]) -> str:
    """由 Pydantic schema 生成最小合法 JSON（str→""，int→0，list→[]，enum→首个取值）。"""
```

**`__all__`（`llm/__init__.py`，字母序）**：

```python
__all__ = [
    "LlmConfigError",
    "LlmSettings",
    "build_chat_model",
    "clear_llm_cache",
    "get_chat_model",
    "invoke_structured",
]
```

---

## ⑥ 实现规范

1. 🔴 **单一构造点**：全仓构造 ChatModel 的地方**只有** `factory.build_chat_model`。节点里出现 `ChatOpenAI(...)` / `OpenAI(...)` 视为越界。
2. 🔴 **缓存键必须是原始标量元组**（`provider, model, temperature, max_tokens, api_key, base_url`），**不得**直接 `lru_cache` 一个 `LlmSettings` 参数——Pydantic `BaseSettings` 实例**不可哈希**，会直接 `TypeError`。
3. 🔴 **默认 `temperature=0`**：审核与改写要求"同输入同输出"，评估与回归才有意义。
4. 🔴 **离线桩必须真的能被 `with_structured_output` 解析**：产出前用 `schema.model_json_schema()` 决定字段形状（含 `enum` / 数组 / `Optional`），**不要**返回 `"{}"`（必被校验拒绝，导致离线图跑不通）。
5. 🔴 **运行期不抛异常**：`invoke_structured` 捕获 `Exception` → 记 `log_event(..., level="WARNING")` → 返回 `(None, warnings)`；**禁止**让异常穿透到节点。
6. 🔴 **缺 Key 时构建期立即失败**：`provider="openai"` 且 `api_key` 为空 → `LlmConfigError`（消息里**不得**回显 Key 值）。
7. 🔴 **日志规范**：命名空间 `get_logger("ai_engine.llm.factory")`；事件名 `llm.build` / `llm.structured.ok` / `llm.structured.degrade`；**必须**带 `trace_id`（由调用方传入；无则用 `"-"`）。
8. ⚠️ `max_tokens` / `timeout_s` / `max_retries` 必须**真正传给**底层模型构造参数；**声明了不消费**是本仓的历史缺陷（标准 §0.4「新增配置必须被消费」）。
9. ⚠️ D2-d 的模型名**只出现在 `LlmSettings.model` 默认值与环境变量**里，节点代码里不得出现模型名字面量。

### 实测后回填（DoD 要求）

| 项 | 实测值（落地时填写） |
|---|---|
| D2-d 最终模型名 | ⬜ 待回填 |
| 结构化输出成功率（同一 schema 连调 10 次） | ⬜ 待回填 |
| 单次 `invoke_structured` 平均耗时 | ⬜ 待回填 |

---

## ⑦ 验收标准（**逐条可执行**）

> 环境前缀见 `AGENTS.md` §4.1。以下断言**全部离线可跑**（不需要网络与 API Key）。

**断言 1 · 默认离线可构造**

```powershell
D:\aanacoda1\python.exe -c "from ai_engine.llm import build_chat_model; from langchain_core.language_models import BaseChatModel; m=build_chat_model(); assert isinstance(m, BaseChatModel); print('offline model ok')"
```

**断言 2 · 离线结构化输出可用**

```powershell
D:\aanacoda1\python.exe -c "from pydantic import BaseModel; from langchain_core.messages import HumanMessage; from ai_engine.llm import build_chat_model, invoke_structured; class T(BaseModel):\n    a: str\n    b: int\nv,w = invoke_structured(build_chat_model(), T, [HumanMessage('x')]); print(v, w); assert isinstance(v, T) and w == []"
```

（PowerShell 下多行 `-c` 不便时的等价做法：写进 `packages/ai-engine/tests/` 的用例，跑 **断言 5**。）

**断言 3 · 缓存同一实例**

```powershell
D:\aanacoda1\python.exe -c "from ai_engine.llm import get_chat_model, clear_llm_cache; clear_llm_cache(); a=get_chat_model('risk'); b=get_chat_model('risk'); assert a is b; c=get_chat_model('risk', None); print('cache ok')"
```

**断言 4 · 缺 API Key 快速失败**

```powershell
D:\aanacoda1\python.exe -c "import os; os.environ['CG_LLM_PROVIDER']='openai'; os.environ.pop('CG_LLM_API_KEY', None); from ai_engine.llm import build_chat_model, LlmConfigError; from ai_engine.llm.settings import LlmSettings\n try:\n    build_chat_model(LlmSettings())\n except LlmConfigError as e:\n    print('expected:', e)"
```

期望：打印 `expected: ...`，**不得**发起网络请求。

**断言 5 · 测试基线**

```powershell
D:\aanacoda1\python.exe -m pytest packages/ai-engine -q
```

期望：新增用例全绿；全量 `pytest` 仍 **≥ 140 passed**。

---

## ⑧ 测试要求

`packages/ai-engine/tests/test_llm_factory.py`：

| 用例 | 断言 |
|---|---|
| `test_default_settings_are_offline` | `LlmSettings().provider == "offline"`；构造不读 Key |
| `test_build_offline_model_is_base_chat_model` | 返回 `BaseChatModel` 实例 |
| `test_offline_structured_output_roundtrip` | 含 `str/int/list/Optional/enum` 的 schema → 得到实例且字段类型正确 |
| `test_invoke_structured_degrades_without_raising` | 注入返回非法 JSON 的桩模型 → `(None, warnings)`，warnings 非空，**无异常** |
| `test_get_chat_model_caches_by_role` | 同 role 同配置 → 同一对象；`clear_llm_cache()` 后 → 新对象 |
| `test_temperature_override_changes_cache_key` | `temperature=0.7` 与默认 `0.0` 得到**不同**实例 |
| `test_openai_without_key_raises_config_error` | `LlmConfigError`；且**未发生网络调用**（用 `monkeypatch` 断言 `httpx` 未被触碰） |
| `test_settings_env_prefix` | `monkeypatch.setenv("CG_LLM_MAX_TOKENS", "128")` → `LlmSettings().max_tokens == 128` |

> 🔴 全部用例**禁止**真实调用外部模型。需要"真实模型"的用例一律 `skipif`（标准 §0.6.2，环境变量 `CG_LLM_TEST_OPENAI=1`）。

---

## ⑨ 避坑清单

| # | 坑 | 说明 |
|---|---|---|
| 1 | 把 `LlmConfigError` 的**类定义**写进 `rag_contracts/errors.py` | 契约层只保留 `RagError` 基类；具体异常留在使用方（`ai_engine/llm/errors.py`），**继承**基类即可，不新增错误码常量 |
| 2 | `@lru_cache` 直接套在 `build_chat_model(settings: LlmSettings)` 上 | `BaseSettings` 不可哈希 → `TypeError`。用标量元组做键（§⑥.2） |
| 3 | 离线桩返回 `"{}"` | 结构化校验必失败，整个离线图跑不通。必须按 schema 生成最小合法 JSON |
| 4 | 在节点里 `ChatOpenAI(...)` | 违反单一构造点；模型名会散落到各处，D2-d 变更要改 N 个文件 |
| 5 | `temperature` 默认给 0.7 | 审核结果不可复现，评测无法回归 |
| 6 | 把 `api_key` 写进 `.env.example` 或测试 | 泄露风险。`.env.example` 只写**注释与空值** |
| 7 | 让 `invoke_structured` 抛异常 | 违背标准 §0.5.2 的降级红线；节点会整条链路失败 |
| 8 | 用 `langchain.chat_models` 这类**旧路径** | 本仓实测栈是 `langchain-core 1.6` + `langgraph 1.2`，旧 import 路径可能不存在（`2-1` 的断言 1 会先暴露） |