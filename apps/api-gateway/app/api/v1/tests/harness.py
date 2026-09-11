"""API v1 测试专属 Mock Harness。

生产路由已落地 Walking Skeleton：各核心端点（contracts/tasks/config/report/
feedback/agent）通过 ``response_model`` 返回 ``MockDataFactory`` 的 Pydantic 实例，
JSON 输出为 camelCase（alias）。

本 harness 从「真实模块 router 的路由表」读取 methods + path（只读对齐契约路径），
为这些端点注册「测试专用 Mock Handler」，从而可在不改生产逻辑的前提下真实复现
边界行为：422 Schema 校验、404 资源不存在、空数组/分页溢出、任务状态机。

对齐策略：
- 成功响应体委托 ``MockDataFactory`` 并以 ``model_dump(by_alias=True, mode="json")``
  序列化，确保与生产 ``response_model`` 输出逐字段一致：统一包络
  ``{code:'SUCCESS', traceId, serverTime, data}``（camelCase，无 message 字段）。
- 边界行为（404、空数组、分页溢出、四态任务、.exe 拒收）由 harness 自行模拟，
  并手工包上同样的统一包络（空数组语义位于 data 内；四态任务的进度对象嵌在
  data 内），覆盖生产默认数据。
- 请求体使用 ``app.generated.schemas`` 中的 Pydantic 模型（如 UploadBody、
  AgentRewriteRequest），以真实触发 422 Schema 校验。

- 该文件是测试基础设施，禁止在生产 import。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.routing import APIRoute

from app.api.v1 import agent, config as config_router, contracts, feedback, report, tasks
from app.generated.schemas import (
    AgentRewriteRequest,
    CommitBody,
    FeedbackCreateBody,
    UploadBody,
)
from app.services.mock_factory import MockDataFactory

# ---- 知识库常量：已知/未知资源（标准 UUID 格式）----
KNOWN_CONTRACT = "00000000-0000-0000-0000-000000000001"
KNOWN_TASK = "00000000-0000-0000-0000-000000000002"
KNOWN_RULE = "00000000-0000-0000-0000-000000000003"
KNOWN_FEEDBACK = "00000000-0000-0000-0000-000000000004"
KNOWN_CLAUSE = "00000000-0000-0000-0000-000000000020"

# ---- 任务状态机 Store（PENDING -> PROCESSING -> COMPLETED / FAILED）----
TASK_STORE: dict[str, str] = {}

STAGE_BY_STATUS = {
    "PENDING": ("queued", 0.0),
    "PROCESSING": ("processing", 0.6),
    "COMPLETED": ("done", 1.0),
    "FAILED": ("failed", 0.0),
}


def set_task_state(task_id: str, status: str) -> None:
    """推进/设置一个任务的状态。"""
    TASK_STORE[str(task_id)] = status


def reset_task_states() -> None:
    """每个用例后清理，避免跨用例污染。"""
    TASK_STORE.clear()


def build_progress(task_id: str, status: str) -> dict:
    """按四态映射任务进度（percent 采用 [0,1]）。"""
    stage, percent = STAGE_BY_STATUS[status]
    progress = {
        "taskId": task_id,
        "status": status,
        "stage": stage,
        "percent": percent,
        "result": None,
    }
    if status == "COMPLETED":
        progress["result"] = {"impactedClauseCount": 3}
    return progress


def _dump(model) -> dict:
    """Pydantic 实例 -> camelCase JSON dict（对齐生产 response_model 序列化）。"""
    return model.model_dump(by_alias=True, mode="json")


def _envelope_dict(data) -> dict:
    """手工构造统一包络 dict：``{code:'SUCCESS', traceId, serverTime, data}``。

    用于无法走生成 wrapper 模型的边界语义——任务四态使用 PENDING/PROCESSING/
    COMPLETED，超出 schemas.Status 枚举（RUNNING/DONE/FAILED），不能实例化
    TaskProgressResponse，故进度对象以手工构造的包络嵌在 data 内。
    """
    return {
        "code": "SUCCESS",
        "traceId": "test-trace",
        "serverTime": "2026-08-18T00:00:00Z",
        "data": data,
    }


# ============================== Mock Handlers ==============================
# 签名与原端点一致（含 Query/UUID 约束 + Pydantic body），确保 422 能真实触发。
async def mock_list_contracts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contract_type: str | None = None,
    status: str | None = None,
) -> dict:
    # 分页边界：请求超出现有页数 -> data.items 为空数组而非 null
    if page > 1:
        return _envelope_dict(
            {"items": [], "total": 0, "page": page, "pageSize": page_size, "hasMore": False}
        )
    return _dump(MockDataFactory.list_contracts(page, page_size))


async def mock_presign_upload(body: dict) -> dict:
    return _dump(MockDataFactory.presign_upload())


async def mock_upload_contract(body: UploadBody) -> dict:
    # 边界：拒收 .exe / .zip 文件类型
    suffix = body.file_name.lower()
    if suffix.endswith(".exe") or suffix.endswith(".zip"):
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix}")
    return _dump(MockDataFactory.upload_contract(body))


async def mock_get_contract(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.get_contract(contract_id))


async def mock_get_parse_status(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.get_parse_status(contract_id))


async def mock_get_elements(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.get_elements(contract_id))


async def mock_get_risks(contract_id: UUID, level: str | None = None) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    # 无风险条款：data 必须是空数组 [] 而非 null（包络内）
    return _envelope_dict([])


async def mock_get_conflicts(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    # 无矛盾条款：data 必须是空数组 [] 而非 null（包络内）
    return _envelope_dict([])


async def mock_get_seals(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.get_seals(contract_id))


async def mock_commit_contract(contract_id: UUID, body: CommitBody) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.commit_contract(contract_id, body))


# ---- Tasks ----
async def mock_get_task(task_id: UUID) -> dict:
    tid = str(task_id)
    if tid != KNOWN_TASK:
        raise HTTPException(status_code=404, detail="task not found")
    status = TASK_STORE.get(tid, "PENDING")
    # 四态进度对象嵌在包络 data 内（PENDING/PROCESSING/COMPLETED 超出 Status
    # 枚举，无法走 TaskProgressResponse，故手工构造包络）
    return _envelope_dict(build_progress(tid, status))


async def mock_stream_task(task_id: UUID) -> dict:
    if str(task_id) != KNOWN_TASK:
        raise HTTPException(status_code=404, detail="task not found")
    # SSE stream 结构（harness 以 JSON 近似）：事件为裸 TaskProgress 形状
    return {"taskId": str(task_id), "events": [build_progress(str(task_id), TASK_STORE.get(str(task_id), "PENDING"))]}


# ---- Config ----
async def mock_list_rules(contract_type: str | None = None) -> dict:
    return _dump(MockDataFactory.list_rules(contract_type))


async def mock_get_rule(config_id: str) -> dict:
    if config_id != KNOWN_RULE:
        raise HTTPException(status_code=404, detail="config not found")
    return _dump(MockDataFactory.get_rule(config_id))


async def mock_update_rule(config_id: str, body: dict) -> dict:
    if config_id != KNOWN_RULE:
        raise HTTPException(status_code=404, detail="config not found")
    if str(body.get("version")) != "1":
        raise HTTPException(status_code=409, detail="version conflict")
    return _dump(MockDataFactory.update_rule(config_id, body))


# ---- Report ----
async def mock_generate_report(contract_id: UUID, body: dict) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.generate_report(contract_id, body))


async def mock_get_report(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return _dump(MockDataFactory.get_report(contract_id))


# ---- Feedback ----
async def mock_create_feedback(body: dict) -> dict:
    if (body or {}).get("dup"):
        raise HTTPException(status_code=409, detail="duplicate feedback")
    return _dump(MockDataFactory.create_feedback(FeedbackCreateBody.model_validate(body)))


# ---- Agent（超长文本 / Prompt 注入 / XSS 等边界输入不得导致异常）----
async def mock_agent_rewrite(body: AgentRewriteRequest) -> dict:
    return _dump(MockDataFactory.rewrite_clause(body))


# ---- 路由表：methods+path -> Mock Handler ----
MOCK_ROUTES: dict[tuple[str, str], object] = {
    ("GET", "/contracts"): mock_list_contracts,
    ("POST", "/contracts/presign"): mock_presign_upload,
    ("POST", "/contracts/upload"): mock_upload_contract,
    ("GET", "/contracts/{contract_id}"): mock_get_contract,
    ("GET", "/contracts/{contract_id}/parse-status"): mock_get_parse_status,
    ("GET", "/contracts/{contract_id}/elements"): mock_get_elements,
    ("GET", "/contracts/{contract_id}/risks"): mock_get_risks,
    ("GET", "/contracts/{contract_id}/conflicts"): mock_get_conflicts,
    ("GET", "/contracts/{contract_id}/seals"): mock_get_seals,
    ("POST", "/contracts/{contract_id}/commit"): mock_commit_contract,
    ("GET", "/tasks/{task_id}"): mock_get_task,
    ("GET", "/tasks/{task_id}/stream"): mock_stream_task,
    ("GET", "/settings/rules"): mock_list_rules,
    ("GET", "/settings/rules/{config_id}"): mock_get_rule,
    ("PUT", "/settings/rules/{config_id}"): mock_update_rule,
    ("POST", "/report/{contract_id}/generate"): mock_generate_report,
    ("GET", "/report/{contract_id}"): mock_get_report,
    ("POST", "/feedback"): mock_create_feedback,
    ("POST", "/agent/rewrite"): mock_agent_rewrite,
}

# 参与 Mock 的模块 router（路径表只读对齐，处理逻辑由 harness 接管）
_MOCK_MODULES = [
    ("Contracts", contracts.router),
    ("Tasks", tasks.router),
    ("Config", config_router.router),
    ("Report", report.router),
    ("Feedback", feedback.router),
    ("Agent", agent.router),
]


def build_test_app() -> FastAPI:
    """构建测试应用：真实路径 + Mock 处理逻辑。"""
    api = APIRouter(prefix="/api/v1")

    seen: set[tuple[str, str]] = set()
    for _tag, module_router in _MOCK_MODULES:
        for route in module_router.routes:
            if not isinstance(route, APIRoute):
                continue
            methods = sorted(m for m in route.methods if m in {"GET", "POST", "PUT", "PATCH", "DELETE"})
            key = (methods[0], route.path)
            handler = MOCK_ROUTES.get(key)
            if handler is not None and key not in seen:
                seen.add(key)
                api.add_api_route(
                    route.path,
                    handler,
                    methods=methods,
                    include_in_schema=False,
                )

    app = FastAPI(title="api-v1-edge-case-harness")
    app.include_router(api)
    return app
