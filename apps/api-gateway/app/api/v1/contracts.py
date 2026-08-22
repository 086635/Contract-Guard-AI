"""Contracts 路由（合同上传 / 解析 / 要素 / 风险 / 矛盾 / 印章）。

对应 data_scheme.md §5.2 级联时序、§7.3 端点清单、§8.1/§8.2/§8.4。

NOTE：以下为骨架路由，业务逻辑（services / db / worker 调用）待实现。
类型模型待 `pnpm --filter @contract-guard/schema gen:py` 生成后从
`app.generated.schemas` 引入并绑定 response_model。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/contracts", tags=["Contracts"])


@router.get("")
async def list_contracts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contract_type: str | None = None,
    status: str | None = None,
) -> dict:
    """合同列表（工作台历史，§5.1）。"""
    raise HTTPException(status_code=501, detail="list_contracts 待实现")


@router.post("/presign")
async def presign_upload(body: dict) -> dict:
    """获取直传凭证（上传三步法 步骤1，§5.3）。"""
    raise HTTPException(status_code=501, detail="presign_upload 待实现")


@router.post("/upload")
async def upload_contract(body: dict) -> dict:
    """触发解析任务（上传三步法 步骤3，§5.3）。需 X-Request-ID 幂等。"""
    raise HTTPException(status_code=501, detail="upload_contract 待实现")


@router.get("/{contract_id}")
async def get_contract(contract_id: str) -> dict:
    """合同详情。"""
    raise HTTPException(status_code=501, detail="get_contract 待实现")


@router.get("/{contract_id}/parse-status")
async def get_parse_status(contract_id: str) -> dict:
    """解析状态轮询（§5.2-B）。"""
    raise HTTPException(status_code=501, detail="get_parse_status 待实现")


@router.get("/{contract_id}/elements")
async def get_elements(contract_id: str) -> dict:
    """要素抽取（§5.2-C）。"""
    raise HTTPException(status_code=501, detail="get_elements 待实现")


@router.get("/{contract_id}/risks")
async def get_risks(contract_id: UUID, level: str | None = None) -> dict:
    """风险列表（§5.2-D / §8.4 EnhancedRiskItem）。"""
    raise HTTPException(status_code=501, detail="get_risks 待实现")


@router.get("/{contract_id}/conflicts")
async def get_conflicts(contract_id: str) -> dict:
    """跨条款矛盾（§5.2-E / §8.2）。"""
    raise HTTPException(status_code=501, detail="get_conflicts 待实现")


@router.get("/{contract_id}/seals")
async def get_seals(contract_id: str) -> dict:
    """印章签名合规（§5.2-F / §8.1）。"""
    raise HTTPException(status_code=501, detail="get_seals 待实现")


@router.post("/{contract_id}/commit")
async def commit_contract(contract_id: UUID, body: dict) -> dict:
    """采纳修改 / 新版本（§5.2 收尾，需 version 乐观锁）。"""
    raise HTTPException(status_code=501, detail="commit_contract 待实现")
