"""OpenAPI 合同 DTO 投影器。

设计要点：
1. 所有表级 DTO → API DTO 的缺失字段规则集中在本文件。
2. 健康分只由风险/矛盾数据确定，不使用随机值。
3. 路由和业务 Service 不直接拼装 Contract/ContractSummary。

对应：`文件集合/codex/4.C12合同摘要与详情服务.md`。
"""

from __future__ import annotations

import math
from pathlib import Path

from ai_engine.agents.auditor import EnhancedRiskItem as AuditRiskItem
from ai_engine.agents.conflict import CrossClauseConflict as AuditConflict
from ai_engine.graphs.nodes import compute_health_score
from app.db.repository.contract import ContractDetail
from app.db.repository.result import ReportRecord
from app.generated import schemas as s
from rag_contracts.chunk import BBox, ContractChunk


class ContractProjector:
    """合同持久化 DTO 到 OpenAPI DTO 的唯一投影器。"""

    @staticmethod
    def summary(
        detail: ContractDetail,
        risks: list[s.EnhancedRiskItem],
        conflicts: list[s.CrossClauseConflict],
    ) -> s.ContractSummary:
        """构造合同列表摘要。"""
        health = ContractProjector._health_score(risks, conflicts)
        return s.ContractSummary(
            id=str(detail.id),
            fileName=detail.filename,
            contractType=detail.contract_type.value,
            status=detail.status,
            healthScore=health,
            riskCount=len(risks),
            createdAt=detail.created_at,
        )

    @staticmethod
    def contract(
        detail: ContractDetail,
        *,
        elements: list[ContractChunk],
        risks: list[s.EnhancedRiskItem],
        conflicts: list[s.CrossClauseConflict],
        seals: list[s.SealSignatureDetection],
        report: ReportRecord | None,
    ) -> s.Contract:
        """构造完整合同详情。"""
        return s.Contract(
            id=str(detail.id),
            fileName=detail.filename,
            fileKey=detail.storage_path,
            fileType=ContractProjector._file_type(detail.filename),
            contractType=detail.contract_type.value,
            typeConfidence=1.0,
            status=detail.status,
            version=detail.version,
            createdById=str(detail.created_by_id) if detail.created_by_id else None,
            legacy=detail.legacy,
            currentVersionId=str(detail.id),
            elements=ContractProjector.elements(elements),
            risks=risks,
            conflicts=conflicts,
            seals=seals,
            missingClauses=[],
            healthScore=ContractProjector._health_score(risks, conflicts),
            reportId=str(report.id) if report is not None else None,
            createdAt=detail.created_at,
            updatedAt=detail.updated_at,
        )

    @staticmethod
    def elements(
        elements: list[ContractChunk],
    ) -> list[s.ContractElement]:
        """批量投影合同切片。"""
        return [ContractProjector._element(element) for element in elements]

    @staticmethod
    def _element(chunk: ContractChunk) -> s.ContractElement:
        bbox = None
        if chunk.bbox is not None and ContractProjector._is_renderable(chunk.bbox):
            bbox = s.BBox(
                page=chunk.bbox.page,
                x=chunk.bbox.x,
                y=chunk.bbox.y,
                width=chunk.bbox.width,
                height=chunk.bbox.height,
                coordinateSystem=s.CoordinateSystem.pdf_points,
            )

        if chunk.category == "CONTRACT_ELEMENT":
            extra = chunk.extra or {}
            value = extra["value"] if "value" in extra else chunk.text
            confidence = float(extra.get("confidence", 1.0))
            is_custom = extra.get("isCustom", False)
            if not isinstance(is_custom, bool):
                raise ValueError(f"要素 {chunk.section_id} 的 isCustom 非法")
            return s.ContractElement(
                key=chunk.section_id,
                label=chunk.title or chunk.section_id,
                value=value,
                confidence=confidence,
                bbox=bbox,
                isCustom=is_custom,
            )

        return s.ContractElement(
            key=chunk.section_id,
            label=chunk.title or chunk.category,
            value=chunk.text,
            confidence=1.0,
            bbox=bbox,
            isCustom=False,
        )

    @staticmethod
    def _is_renderable(bbox: BBox) -> bool:
        """退化框（页码 < 1、宽高非正、坐标非有限）不投影 —— **不伪造高亮**（C60）。"""
        values = (bbox.x, bbox.y, bbox.width, bbox.height)
        if any(not math.isfinite(value) for value in values):
            return False
        return bbox.page >= 1 and bbox.width > 0 and bbox.height > 0

    @staticmethod
    def _health_score(
        risks: list[s.EnhancedRiskItem],
        conflicts: list[s.CrossClauseConflict],
    ) -> s.HealthScore:
        audit_risks = [
            AuditRiskItem.model_validate(risk.model_dump(by_alias=True))
            for risk in risks
        ]
        audit_conflicts = [
            AuditConflict.model_validate(conflict.model_dump(by_alias=True))
            for conflict in conflicts
        ]
        return s.HealthScore.model_validate(
            compute_health_score(audit_risks, audit_conflicts, [])
        )

    @staticmethod
    def _file_type(filename: str) -> s.FileType:
        suffix = Path(filename).suffix.lower()
        if suffix == ".docx":
            return s.FileType.docx
        if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            return s.FileType.image
        return s.FileType.pdf_native


__all__ = ["ContractProjector"]
