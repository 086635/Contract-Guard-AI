"""网关结果入库与审核派发 Service（4.C22 / 4.C23）。

设计要点：
1. 沿用 C21「worker 不直连 DB、gateway 按 Celery 结果落库」模式。
2. 解析入库（C22）：最新 `PARSE_DOC` 任务 DONE 后，把 `clauses` 投影为 `ContractChunk`
   写入 `contract_elements`、把 `sealDetections` 写入 `seal_detections`。
3. 审核派发（C22）：解析入库成功后派发 `run_audit`（幂等，`audit_tasks` 行判重）。
4. 审核入库（C23）：最新 `RUN_AUDIT` 任务 DONE 后，把 `risks` / `conflicts` 写入 `audit_risks`。
5. 运行期不向路由抛异常，收敛为「不入库 + warning」。

对应：`文件集合/codex/4.C22解析结果入库与审核派发.md`、
`文件集合/codex/4.C23审核结果入库.md`。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from app.db.repository.contract import ContractRepository, TaskRepository
from app.db.repository.result import ElementRepository, RiskRepository, SealRepository
from app.generated import schemas as s
from app.services.context import ServiceContext
from app.services.dispatch import TaskKind
from cg_observability import get_logger, get_trace_id, log_event
from rag_contracts.chunk import BBox, ContractChunk
from rag_contracts.enums import ChunkType, SourceType

_LOG = get_logger("api_gateway.services.ingestion")


def _optional_int(value: Any) -> int | None:
    """宽松转 int，非法或缺省返回 None。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    """宽松转 str，缺省返回 None。"""
    if value is None:
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    """宽松转 float，非法或缺省返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clause_bbox(clause: dict[str, Any]) -> BBox | None:
    """把 clause 的 `page_no + bbox` 合并为 `ContractChunk.BBox`（缺几何返回 None）。"""
    raw = clause.get("bbox")
    if not isinstance(raw, dict):
        return None
    page = _optional_int(raw.get("page")) or _optional_int(clause.get("page_no"))
    if page is None:
        return None
    try:
        return BBox(
            page=page,
            x=float(raw["x"]),
            y=float(raw["y"]),
            width=float(raw["width"]),
            height=float(raw["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clause_payload_to_chunk(
    contract_id: str,
    clause: dict[str, Any],
    seq: int,
) -> ContractChunk:
    """把解析层 clause dict 投影为 `ContractChunk`（唯一处，见 `4.C22 §⑤.1`）。

    Args:
        contract_id: 合同 ID（`doc_id` 与 `chunk_id` 命名空间）。
        clause: 解析层条款 dict（键名见 `worker/parse_pipeline.py::clause_payloads`）。
        seq: 文档内顺序号（1 起）。

    Returns:
        校验通过的 `ContractChunk`。

    Raises:
        ValueError: `clause_id` / `text` 缺失。
    """
    clause_id = str(clause.get("clause_id") or clause.get("id") or "").strip()
    if not clause_id:
        raise ValueError("clause_id 缺失")
    text = str(clause.get("text") or "").strip()
    if not text:
        raise ValueError(f"条款 {clause_id} 缺少 text")

    label = str(clause.get("label") or clause.get("section_id") or clause_id).strip()
    bbox = _clause_bbox(clause)
    page_no = _optional_int(clause.get("page_no"))
    if page_no is None and bbox is not None:
        page_no = bbox.page

    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id(contract_id, clause_id, 0),
        doc_id=contract_id,
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.CLAUSE,
        section_id=clause_id,
        section_path=label,
        seq_in_doc=seq,
        title=label,
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=max(1, round(len(text) / 1.5)),
        level=int(_optional_int(clause.get("level")) or 0),
        parent_section_id=_optional_str(clause.get("parent_id")),
        page_no=page_no,
        bbox=bbox,
    )


def element_payload_to_chunk(
    contract_id: str, element: dict[str, Any]
) -> ContractChunk:
    """把解析层 element dict 投影为带 extra 的 ContractChunk。"""
    key = str(element.get("key") or "").strip()
    if not key:
        raise ValueError("element.key 缺失")
    label = str(element.get("label") or key).strip()
    raw_value = element.get("value")
    if raw_value is not None and not isinstance(raw_value, str):
        raise ValueError(f"element {key} 的 value 必须是 string 或 null")
    confidence = _optional_float(element.get("confidence"))
    if confidence is None or confidence < 0.0 or confidence > 1.0:
        raise ValueError(f"element {key} 的 confidence 非法")
    is_custom = element.get("isCustom", False)
    if not isinstance(is_custom, bool):
        raise ValueError(f"element {key} 的 isCustom 必须是 boolean")

    bbox = _clause_bbox({"bbox": element.get("bbox")})
    text = raw_value if raw_value else label
    return ContractChunk(
        chunk_id=ContractChunk.make_chunk_id(contract_id, key, 0),
        doc_id=contract_id,
        source_type=SourceType.STANDARD_CLAUSE,
        chunk_type=ChunkType.PARAGRAPH,
        section_id=key,
        section_path=f"{contract_id}/{key}",
        seq_in_doc=0,
        title=label,
        text=text,
        text_hash=ContractChunk.compute_text_hash(text),
        char_count=len(text),
        token_count=max(1, round(len(text) / 1.5)),
        level=0,
        page_no=bbox.page if bbox is not None else None,
        bbox=bbox,
        category="CONTRACT_ELEMENT",
        extra={
            "value": raw_value,
            "confidence": confidence,
            "isCustom": is_custom,
        },
    )


class IngestionService:
    """网关侧惰性结果入库与审核派发。"""

    def __init__(
        self,
        context: ServiceContext,
        *,
        elements: ElementRepository | None = None,
    ) -> None:
        """构造服务；`elements` 可注入替身（坐标重建用例需要**离线**验证写回）。"""
        self._context = context
        self._contracts = ContractRepository(context.session)
        self._tasks = TaskRepository(context.session)
        self._elements = elements or ElementRepository(context.session)
        self._risks = RiskRepository(context.session)
        self._seals = SealRepository(context.session)

    async def finalize_parse(self, contract_id: UUID) -> bool:
        """按最新 `PARSE_DOC` 任务结果入库，并触发审核派发。

        Args:
            contract_id: 合同 ID。

        Returns:
            True 表示存在 DONE 快照并尝试入库；False 表示无任务 / 非 DONE / 无快照。
        """
        task = await self._tasks.get_latest_by_contract(
            contract_id,
            TaskKind.PARSE_DOC.name,
        )
        if task is None:
            return False

        snapshot = self._context.task_dispatcher.get_status(
            task.celery_task_id or str(task.id),
        )
        if snapshot is None or snapshot.status != "DONE" or snapshot.result is None:
            return False

        result = snapshot.result
        clauses = [
            clause
            for clause in (result.get("clauses") or [])
            if isinstance(clause, dict)
        ]
        seals_raw = [
            seal
            for seal in (result.get("sealDetections") or [])
            if isinstance(seal, dict)
        ]
        elements_raw = [
            element
            for element in (result.get("elements") or [])
            if isinstance(element, dict)
        ]

        chunks, chunk_warnings = self._build_chunks(str(contract_id), clauses)
        element_chunks, element_warnings = self._build_element_chunks(
            str(contract_id),
            elements_raw,
        )
        seals, seal_warnings = self._build_seals(seals_raw)
        warnings = [*chunk_warnings, *element_warnings, *seal_warnings]

        changed = False
        if chunks or element_chunks:
            await self._elements.bulk_upsert(
                contract_id,
                [*chunks, *element_chunks],
            )
            changed = True
        if seals:
            await self._seals.bulk_upsert(contract_id, seals)
            changed = True
        if changed:
            await self._context.session.commit()

        dispatched: str | None = None
        if chunks:
            try:
                dispatched = await self.ensure_audit_dispatched(contract_id, clauses)
            except Exception as exc:  # noqa: BLE001 - 派发失败不阻断读取链路
                warnings.append(f"审核派发失败：{type(exc).__name__}: {exc}")
                log_event(
                    _LOG,
                    "ingestion.parse.dispatch_failed",
                    level="WARNING",
                    trace_id=get_trace_id(),
                    contract_id=str(contract_id),
                    error=repr(exc),
                )

        log_event(
            _LOG,
            "ingestion.parse.finalize",
            trace_id=get_trace_id(),
            contract_id=str(contract_id),
            chunks=len(chunks),
            elements=len(element_chunks),
            seals=len(seals),
            dispatched=dispatched,
            warnings=len(warnings),
        )
        return True

    async def finalize_preview_alignment(
        self,
        contract_id: UUID,
        chunks: Sequence[ContractChunk],
    ) -> int:
        """把重建后的切片坐标写回 `contract_elements`（C60 §④）。

        `chunk_id` 不变，故 `bulk_upsert` 是**幂等更新**：重复重建得到同样的坐标，
        写库结果一致（断言 4）。

        Args:
            contract_id: 合同 ID。
            chunks: 已重建坐标的切片（只改 `bbox` / `page_no`）。

        Returns:
            实际写入的切片数；空输入不触发任何写。
        """
        if not chunks:
            return 0
        await self._elements.bulk_upsert(contract_id, chunks)
        return len(chunks)

    async def finalize_audit(self, contract_id: UUID) -> bool:
        """按最新 `RUN_AUDIT` 任务结果入库 `risks` / `conflicts`。

        Args:
            contract_id: 合同 ID。

        Returns:
            True 表示存在 DONE 快照并尝试入库；False 表示无任务行 / 无 celery id /
            非 DONE / 无快照。
        """
        task = await self._tasks.get_latest_by_contract(
            contract_id,
            TaskKind.RUN_AUDIT.name,
        )
        if task is None or not task.celery_task_id:
            return False

        snapshot = self._context.task_dispatcher.get_status(task.celery_task_id)
        if snapshot is None or snapshot.status != "DONE" or snapshot.result is None:
            return False
        result_status = (
            snapshot.result.get("status") if isinstance(snapshot.result, dict) else None
        )
        if result_status == "FAILED":
            log_event(
                _LOG,
                "ingestion.audit.failed",
                level="WARNING",
                trace_id=get_trace_id(),
                contract_id=str(contract_id),
                task_id=str(task.id),
            )
            return False

        risks, conflicts, warnings = self._build_audit_results(snapshot.result)
        if risks or conflicts:
            await self._risks.bulk_upsert(contract_id, [*risks, *conflicts])
            await self._context.session.commit()

        log_event(
            _LOG,
            "ingestion.audit.finalize",
            trace_id=get_trace_id(),
            contract_id=str(contract_id),
            risks=len(risks),
            conflicts=len(conflicts),
            warnings=len(warnings),
        )
        return True

    async def ensure_audit_dispatched(
        self,
        contract_id: UUID,
        clause_payload: list[dict[str, Any]],
    ) -> str | None:
        """派发 `run_audit` 并建 `audit_tasks` 行（幂等：已有任务行即复用）。"""
        existing = await self._tasks.get_latest_by_contract(
            contract_id,
            TaskKind.RUN_AUDIT.name,
        )
        if existing is not None and existing.celery_task_id:
            return existing.celery_task_id

        celery_task_id = self._context.task_dispatcher.send(
            TaskKind.RUN_AUDIT,
            args=(str(contract_id), clause_payload),
        )
        await self._tasks.create(
            contract_id,
            TaskKind.RUN_AUDIT.name,
            celery_task_id,
        )
        await self._context.session.commit()
        return celery_task_id

    @staticmethod
    def _build_element_chunks(
        contract_id: str,
        elements: list[dict[str, Any]],
    ) -> tuple[list[ContractChunk], list[str]]:
        """逐条投影解析要素，失败隔离为 warning。"""
        chunks: list[ContractChunk] = []
        warnings: list[str] = []
        for element in elements:
            try:
                chunks.append(element_payload_to_chunk(contract_id, element))
            except (ValueError, TypeError) as exc:
                warnings.append(f"要素投影失败：{exc}")
        return chunks, warnings

    @staticmethod
    def _build_chunks(
        contract_id: str,
        clauses: list[dict[str, Any]],
    ) -> tuple[list[ContractChunk], list[str]]:
        """逐条投影条款，失败隔离为 warning。"""
        chunks: list[ContractChunk] = []
        warnings: list[str] = []
        for seq, clause in enumerate(clauses, start=1):
            try:
                chunks.append(clause_payload_to_chunk(contract_id, clause, seq))
            except (ValueError, TypeError) as exc:
                warnings.append(f"条款投影失败：{exc}")
        return chunks, warnings

    @staticmethod
    def _build_audit_results(
        result: dict[str, Any],
    ) -> tuple[list[s.EnhancedRiskItem], list[s.CrossClauseConflict], list[str]]:
        """把审核快照顶层的 `risks` / `conflicts` 校验为 DTO，失败隔离为 warning。"""
        risks: list[s.EnhancedRiskItem] = []
        conflicts: list[s.CrossClauseConflict] = []
        warnings: list[str] = []
        for raw in result.get("risks") or []:
            if not isinstance(raw, dict):
                warnings.append("risk 非 dict，已跳过")
                continue
            try:
                risks.append(s.EnhancedRiskItem.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 - 单条风险失败隔离
                warnings.append(f"风险投影失败：{type(exc).__name__}")
        for raw in result.get("conflicts") or []:
            if not isinstance(raw, dict):
                warnings.append("conflict 非 dict，已跳过")
                continue
            try:
                conflicts.append(s.CrossClauseConflict.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 - 单条矛盾失败隔离
                warnings.append(f"矛盾投影失败：{type(exc).__name__}")
        return risks, conflicts, warnings

    @staticmethod
    def _build_seals(
        seals_raw: list[dict[str, Any]],
    ) -> tuple[list[s.SealSignatureDetection], list[str]]:
        """把 openapi `SealSignatureDetection` 形状的 dict 校验为 DTO。"""
        seals: list[s.SealSignatureDetection] = []
        warnings: list[str] = []
        for raw in seals_raw:
            try:
                seals.append(s.SealSignatureDetection.model_validate(raw))
            except Exception as exc:  # noqa: BLE001 - 单条印章失败隔离
                warnings.append(f"印章投影失败：{type(exc).__name__}")
        return seals, warnings


__all__ = [
    "IngestionService",
    "clause_payload_to_chunk",
]
