"""C70 标准条款库管理路由（ADMIN-only）。

契约见 `packages/schema/openapi.yaml`：`/admin/standard-clauses*`。
写入/启停/重建均记审计（`SETTINGS_UPDATE` + `STANDARD_CLAUSE`）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.deps import (
    get_audit_service,
    get_service_context,
    get_standard_clause_service,
    require_roles,
    write_audit_event,
)
from app.db.models import AuditAction, AuditOutcome
from app.db.repository.auth import UserRecord
from app.db.repository.standard_clause import (
    StandardClauseCreate,
    StandardClauseNotFoundError,
    StandardClausePatch,
    StandardClauseRecord,
    StandardClauseVersionConflictError,
)
from app.generated.schemas import (
    ContractType,
    Role,
    StandardClause,
    StandardClauseCreateRequest,
    StandardClauseListData,
    StandardClauseListResponse,
    StandardClauseResponse,
    StandardClauseUpdateRequest,
    TaskProgress,
    TaskProgressResponse,
)
from app.services.audit_service import AuditService
from app.services.context import ServiceContext
from app.services.envelope import build_success_response
from app.services.standard_clause_service import (
    StandardClauseIndexError,
    StandardClauseService,
    StandardClauseValidationError,
)

router = APIRouter(prefix="/admin", tags=["Admin"])


def _to_dto(record: StandardClauseRecord) -> StandardClause:
    """把仓储记录投影为 OpenAPI StandardClause。"""
    return StandardClause(
        id=str(record.id),
        title=record.title,
        contractType=record.contract_type,
        text=record.text,
        isEnabled=record.is_enabled,
        version=record.version,
        source=record.source,
        updatedAt=record.updated_at,
    )


@router.get(
    "/standard-clauses",
    response_model=StandardClauseListResponse,
    status_code=200,
)
async def list_standard_clauses(
    _admin: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    service: Annotated[StandardClauseService, Depends(get_standard_clause_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 20,
    contract_type: Annotated[ContractType | None, Query(alias="contractType")] = None,
    is_enabled: Annotated[bool | None, Query(alias="isEnabled")] = None,
    query: Annotated[str | None, Query()] = None,
) -> StandardClauseListResponse:
    """ADMIN 分页查询标准条款库（含停用条目）。"""
    records, total = await service.list(
        page,
        page_size,
        query=query,
        contract_type=contract_type,
        is_enabled=is_enabled,
    )
    data = StandardClauseListData(
        items=[_to_dto(record) for record in records],
        total=total,
        page=page,
        pageSize=page_size,
        hasMore=page * page_size < total,
    )
    return build_success_response(StandardClauseListResponse, data)


@router.post(
    "/standard-clauses",
    response_model=StandardClauseResponse,
    status_code=201,
)
async def create_standard_clause(
    body: StandardClauseCreateRequest,
    request: Request,
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: Annotated[ServiceContext, Depends(get_service_context)],
    service: Annotated[StandardClauseService, Depends(get_standard_clause_service)],
) -> StandardClauseResponse:
    """新建标准条款并同步写入现有 RAG STANDARD_CLAUSE 索引。"""
    try:
        record = await service.create(
            StandardClauseCreate(
                title=body.title,
                contract_type=body.contract_type,
                text=body.text,
                source=body.source or "manual",
            )
        )
    except StandardClauseIndexError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.settings_update,
            "STANDARD_CLAUSE",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            metadata={"operation": "CREATE", "errorCode": "INDEX_UNAVAILABLE"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="standard clause index unavailable",
        ) from exc
    await write_audit_event(
        audit,
        request,
        AuditAction.settings_update,
        "STANDARD_CLAUSE",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(record.id),
        metadata={
            "operation": "CREATE",
            "contractType": record.contract_type.value,
        },
    )
    await context.session.commit()
    return build_success_response(StandardClauseResponse, _to_dto(record))


@router.post(
    "/standard-clauses/rebuild",
    response_model=TaskProgressResponse,
    status_code=200,
)
async def rebuild_standard_clause_index(
    request: Request,
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: Annotated[ServiceContext, Depends(get_service_context)],
    service: Annotated[StandardClauseService, Depends(get_standard_clause_service)],
) -> TaskProgressResponse:
    """全量重建标准条款检索索引（幂等、可重试）。"""
    try:
        result = await service.rebuild_index()
    except StandardClauseIndexError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.settings_update,
            "STANDARD_CLAUSE",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            metadata={"operation": "REBUILD_INDEX", "errorCode": "INDEX_UNAVAILABLE"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="standard clause index unavailable",
        ) from exc
    await write_audit_event(
        audit,
        request,
        AuditAction.settings_update,
        "STANDARD_CLAUSE",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        metadata={"operation": "REBUILD_INDEX", **result},
    )
    await context.session.commit()
    progress = TaskProgress(
        taskId="standard-clause-index",
        stage="REBUILD_STANDARD_CLAUSE_INDEX",
        percent=1.0,
        status="DONE",
        result=result,
    )
    return build_success_response(TaskProgressResponse, progress)


@router.patch(
    "/standard-clauses/{clause_id}",
    response_model=StandardClauseResponse,
    status_code=200,
)
async def update_standard_clause(
    clause_id: UUID,
    body: StandardClauseUpdateRequest,
    request: Request,
    user: Annotated[UserRecord, Depends(require_roles(Role.admin))],
    audit: Annotated[AuditService, Depends(get_audit_service)],
    context: Annotated[ServiceContext, Depends(get_service_context)],
    service: Annotated[StandardClauseService, Depends(get_standard_clause_service)],
) -> StandardClauseResponse:
    """更新或启停标准条款；删除语义 = 软停用（isEnabled=false）。"""
    try:
        record = await service.update(
            clause_id,
            StandardClausePatch(
                title=body.title,
                contract_type=body.contract_type,
                text=body.text,
                is_enabled=body.is_enabled,
                version=body.version,
            ),
        )
    except StandardClauseNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="standard clause not found",
        ) from exc
    except StandardClauseVersionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="version conflict",
        ) from exc
    except StandardClauseValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except StandardClauseIndexError as exc:
        await write_audit_event(
            audit,
            request,
            AuditAction.settings_update,
            "STANDARD_CLAUSE",
            AuditOutcome.failure,
            actor_id=user.id,
            actor_role=user.role,
            resource_id=str(clause_id),
            metadata={"operation": "UPDATE", "errorCode": "INDEX_UNAVAILABLE"},
        )
        await context.session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="standard clause index unavailable",
        ) from exc
    await write_audit_event(
        audit,
        request,
        AuditAction.settings_update,
        "STANDARD_CLAUSE",
        AuditOutcome.success,
        actor_id=user.id,
        actor_role=user.role,
        resource_id=str(record.id),
        metadata={
            "operation": "UPDATE",
            "isEnabled": record.is_enabled,
            "version": record.version,
        },
    )
    await context.session.commit()
    return build_success_response(StandardClauseResponse, _to_dto(record))


__all__ = ["router"]
