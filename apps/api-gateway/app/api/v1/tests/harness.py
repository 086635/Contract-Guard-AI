"""API v1 测试专属 Mock Harness。

当前各核心端点（contracts/tasks/config/report/feedback）均为 501 存根。
本 harness 从「真实模块 router 的路由表」读取 methods + path（只读对齐契约路径），
为这些端点注册「测试专用 Mock Handler」，从而可在不改生产逻辑的前提下
真实复现边界行为：422 Schema 校验、404 资源不存在、空数组/分页溢出、任务状态机。

- agent 模块当前亦为 501 存根，一并纳入 Mock（以支撑超长文本/Prompt 注入等边界用例）。
- 该文件是测试基础设施，禁止在生产 import。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.routing import APIRoute

from app.api.v1 import agent, config as config_router, contracts, feedback, report, tasks

# ---- 知识库常量：已知/未知资源 ----
KNOWN_CONTRACT = "00000000-0000-0000-0000-000000000001"
KNOWN_TASK = "00000000-0000-0000-0000-000000000002"

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


# ============================== Mock Handlers ==============================
# 签名与原端点一致（含 Query/UUID 约束），确保 422 能真实触发。
async def mock_list_contracts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contract_type: str | None = None,
    status: str | None = None,
) -> dict:
    # 分页边界：请求超出现有页数 -> 返回空数组而非 null
    if page > 1:
        return {"items": [], "total": 0, "page": page, "pageSize": page_size, "hasMore": False}
    item = {
        "id": KNOWN_CONTRACT,
        "title": "采购合同样本",
        "status": "FINALIZED",
        "contractType": contract_type or "PURCHASE",
        "version": 3,
        "updatedAt": "2026-08-18T00:00:00Z",
    }
    return {"items": [item], "total": 1, "page": page, "pageSize": page_size, "hasMore": True}


async def mock_presign_upload(body: dict) -> dict:
    return {"uploadId": "up-1", "url": "http://minio.test/bucket/x", "method": "PUT", "expiresIn": 600}


async def mock_upload_contract(body: dict) -> dict:
    name = (body or {}).get("fileName") or (body or {}).get("filename") or ""
    suffix = name.lower()
    if suffix.endswith(".exe") or suffix.endswith(".zip"):
        raise HTTPException(status_code=422, detail=f"Unsupported file type: {suffix}")
    if not name:
        raise HTTPException(status_code=422, detail="fileName is required")
    return {"contractId": KNOWN_CONTRACT, "taskId": KNOWN_TASK, "status": "PENDING"}


async def mock_get_contract(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"id": str(contract_id), "title": "采购合同样本", "status": "FINALIZED", "version": 3}


async def mock_get_parse_status(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"contractId": str(contract_id), "status": "DONE", "percent": 1.0}


async def mock_get_elements(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"contractId": str(contract_id), "parties": ["甲方", "乙方"], "amount": "壹佰万元"}


async def mock_get_risks(contract_id: UUID, level: str | None = None) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    # 无风险条款：必须返回空数组 [] 而非 null
    return {"contractId": str(contract_id), "risk_items": [], "total": 0, "hasMore": False}


async def mock_get_conflicts(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"contractId": str(contract_id), "conflicts": [], "total": 0}


async def mock_get_seals(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"contractId": str(contract_id), "seals": []}


async def mock_commit_contract(contract_id: UUID, body: dict) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"contractId": str(contract_id), "version": (body or {}).get("version", 3) + 1}


# ---- Tasks ----
async def mock_get_task(task_id: UUID) -> dict:
    tid = str(task_id)
    if tid != KNOWN_TASK:
        raise HTTPException(status_code=404, detail="task not found")
    status = TASK_STORE.get(tid, "PENDING")
    return build_progress(tid, status)


async def mock_stream_task(task_id: UUID) -> dict:
    if str(task_id) != KNOWN_TASK:
        raise HTTPException(status_code=404, detail="task not found")
    return {"taskId": str(task_id), "events": [build_progress(str(task_id), TASK_STORE.get(str(task_id), "PENDING"))]}


# ---- Config ----
async def mock_list_rules(contract_type: str | None = None) -> dict:
    return {"items": [{"configId": "rule-01", "key": "risk-tolerance", "enabled": True, "contractType": contract_type or "ALL"}]}


async def mock_get_rule(config_id: str) -> dict:
    if config_id != "rule-01":
        raise HTTPException(status_code=404, detail="config not found")
    return {"configId": config_id, "key": "risk-tolerance", "enabled": True}


async def mock_update_rule(config_id: str, body: dict) -> dict:
    if config_id != "rule-01":
        raise HTTPException(status_code=404, detail="config not found")
    if str(body.get("version")) != "1":
        raise HTTPException(status_code=409, detail="version conflict")
    return {"configId": config_id, "key": "risk-tolerance", "enabled": bool(body.get("enabled")), "version": 2}


# ---- Report ----
async def mock_generate_report(contract_id: UUID, body: dict) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"taskId": KNOWN_TASK, "status": "PENDING"}


async def mock_get_report(contract_id: UUID) -> dict:
    if str(contract_id) != KNOWN_CONTRACT:
        raise HTTPException(status_code=404, detail="contract not found")
    return {"contractId": str(contract_id), "status": "DONE", "downloadUrl": "http://minio.test/report.pdf"}


# ---- Feedback ----
async def mock_create_feedback(body: dict) -> dict:
    if (body or {}).get("dup"):
        raise HTTPException(status_code=409, detail="duplicate feedback")
    return {"feedbackId": "fb-1", "status": "APPROVED"}


# ---- Agent（当前 agent.py 为 501 存根，特作 Mock 以支撑超长文本/Prompt 注入边界测试）----
async def mock_agent_rewrite(body: dict) -> dict:
    clause_id = (body or {}).get("clauseId") or "c-1"
    return {
        "taskId": KNOWN_TASK,
        "clauseId": clause_id,
        "before": "乙方应在收到货物后 3 日内付款。",
        "after": "乙方应在收到货物后 15 个工作日内支付首期款项。",
        "reasoningSteps": [
            {"stepNumber": 1, "thought": "定位付款条款", "evidenceText": "第 3.2 条"},
        ],
        "citations": [],
        "confidence": 0.92,
    }


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

# 参与 Mock 的模块 router（agent 除外，agent 复用真实 DI 逻辑）
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