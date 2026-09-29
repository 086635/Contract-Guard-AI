# ============================================================================
# Walking Skeleton Mock 数据工厂（非生产逻辑）
#
# 本文件为 Walking Skeleton 阶段的集中式 Mock 数据工厂：在业务 services /
# db / worker 尚未落地时，为 API 路由与测试提供符合 `app.generated.schemas`
# 契约的真实 Pydantic 实例。
#
# ⚠️  非生产逻辑：所有数据均为内存构造的静态样本，不涉及持久化，
#     不调用 LLM / OCR / RAG，不可用于生产环境。
#
# SSOT：所有 public 方法（stream_task 除外）均返回 app.generated.schemas
#       中的包络 wrapper 实例（继承 ApiEnvelopeBase，统一为
#       {code, traceId, serverTime, data} 四字段，无 message 字段）；
#       payload 字段值遵循业务语义（禁止 "string"/"test" 占位）。
#       stream_task 为 SSE 流式推送豁免包络，逐步产出裸 TaskProgress 事件。
#
# 关于字段命名：任务规范假设 schemas 已启用 populate_by_name=True，
# 但磁盘上 schemas.py 实际未配置该选项（auto-generated, DO NOT EDIT）。
# 故本工厂在实例化时使用 schema 定义的 camelCase 别名（含包络的
# traceId/serverTime，实测 snake_case 传参会报 ValidationError），以保证
# 方法在运行时能成功构造 Pydantic 实例。若后续 gen:py 启用
# populate_by_name，可平滑切换为 snake_case 字段名。
# ============================================================================

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, TypeVar
from uuid import UUID

from app.generated import schemas as s

_EnvelopeT = TypeVar("_EnvelopeT", bound=s.ApiEnvelopeBase)

# ---- 固定常量（合法 UUID，禁止 "string"/"test" 占位）----
_FIXED_CONTRACT_ID = "00000000-0000-0000-0000-000000000001"
_FIXED_TASK_ID = "00000000-0000-0000-0000-000000000002"
_FIXED_RULE_ID = "00000000-0000-0000-0000-000000000003"
_FIXED_FEEDBACK_ID = "00000000-0000-0000-0000-000000000004"
_FIXED_VERSION_ID = "00000000-0000-0000-0000-000000000005"
_FIXED_REPORT_ID = "00000000-0000-0000-0000-000000000006"

_PARTY_A = "北京宏图科技有限公司"
_PARTY_B = "上海盛达贸易股份有限公司"
_FILE_NAME = "采购合同_宏图盛达_202608.docx"
_FILE_KEY = "contracts/2026/08/hongtu-shengda-purchase.docx"


def _now() -> datetime:
    return datetime.now(UTC)


def _trace_id() -> str:
    return f"tr-{uuid.uuid4().hex[:12]}"


def _envelope(wrapper_cls: type[_EnvelopeT], data: Any) -> _EnvelopeT:
    """用生成的 wrapper 类统一包裹 payload。

    统一注入成功包络字段：code=SUCCESS、traceId（tr- 前缀 12 位随机串）、
    serverTime（当前 UTC 时间）。schemas 未启用 populate_by_name，故
    traceId/serverTime 必须以 camelCase 别名传参（实测 snake_case 报
    ValidationError）；code 与 data 字段无 alias，两种写法相同。
    """
    return wrapper_cls(
        code=s.Code.success,
        traceId=_trace_id(),
        serverTime=_now(),
        data=data,
    )


def _bbox(page: int = 1) -> s.BBox:
    return s.BBox(
        page=page,
        x=72.0,
        y=720.0,
        width=468.0,
        height=40.0,
        coordinateSystem=s.CoordinateSystem.pdf_points,
    )


def _dimensions() -> s.Dimensions:
    return s.Dimensions(
        compliance=85.0,
        performanceRisk=30.0,
        clauseCompleteness=90.0,
    )


def _health_score(overall: int = 82) -> s.HealthScore:
    return s.HealthScore(overall=overall, dimensions=_dimensions())


def _citation() -> s.PreciseCitation:
    return s.PreciseCitation(
        sourceType=s.CitationSourceType.law_article,
        sourceTitle="《中华人民共和国民法典》第五百八十五条",
        contentChunk=(
            "当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金；"
            "约定的违约金过分高于造成的损失的，人民法院或者仲裁机构可以根据当事人"
            "的请求予以适当减少。"
        ),
        relevanceScore=0.92,
    )


def _reasoning_steps() -> list[s.ReasoningStep]:
    return [
        s.ReasoningStep(
            stepNumber=1,
            thought="定位付款与违约金条款，识别出明显不利于乙方的违约金比例。",
            evidenceText="第 3.2 条：乙方逾期付款按合同总额 30% 支付违约金。",
        ),
        s.ReasoningStep(
            stepNumber=2,
            thought="比对《民法典》第 585 条，违约金过分高于实际损失，建议调减至实际损失 30% 以内。",
            evidenceText="第 3.2 条违约金比例 vs 民法典 585 条酌减规则。",
        ),
    ]


def _element(
    key: str,
    label: str,
    value: str | None,
    confidence: float = 0.95,
    bbox: s.BBox | None = None,
) -> s.ContractElement:
    return s.ContractElement(
        key=key,
        label=label,
        value=value,
        confidence=confidence,
        bbox=bbox,
        isCustom=False,
    )


def _elements() -> list[s.ContractElement]:
    return [
        _element("partyA", "甲方", _PARTY_A, bbox=_bbox()),
        _element("partyB", "乙方", _PARTY_B, bbox=_bbox()),
        _element("amount", "合同金额", "500000.00 元（人民币伍拾万元整）", bbox=_bbox()),
        _element("signDate", "签署日期", "2026-08-18", bbox=_bbox()),
        _element("deliveryDate", "交货日期", None, confidence=0.0),
    ]


def _risk() -> s.EnhancedRiskItem:
    return s.EnhancedRiskItem(
        id="00000000-0000-0000-0000-000000000010",
        clauseId="00000000-0000-0000-0000-000000000020",
        category=s.RiskCategory.penalty_too_high,
        level=s.RiskLevel.high,
        title="违约金比例过高",
        originalText="乙方逾期付款的，按合同总额 30% 向甲方支付违约金。",
        bbox=_bbox(page=2),
        suggestion="建议将违约金调减至实际损失的 30% 以内，并增设违约金总额上限条款。",
        legalBasis="《民法典》第五百八十五条第二款",
        reasoningSteps=_reasoning_steps(),
        citations=[_citation()],
    )


def _conflict() -> s.CrossClauseConflict:
    return s.CrossClauseConflict(
        id="00000000-0000-0000-0000-000000000030",
        sourceClauseId="00000000-0000-0000-0000-000000000020",
        targetClauseId="00000000-0000-0000-0000-000000000021",
        conflictType=s.ConflictType.payment_contradiction,
        description=(
            "第 3.2 条约定逾期付款违约金为合同总额 30%，与第 6.1 条"
            "“按银行同期贷款利率支付逾期利息”存在金额计算口径矛盾。"
        ),
        level=s.RiskLevel.medium,
        suggestion="统一付款违约责任计算口径，建议以第 3.2 条违约金为主、删除第 6.1 条重复约定。",
        citations=[_citation()],
    )


def _seal() -> s.SealSignatureDetection:
    return s.SealSignatureDetection(
        id="00000000-0000-0000-0000-000000000040",
        type=s.SealType.official_seal,
        status=s.SealStatus.valid,
        bbox=_bbox(page=5),
        page=5,
        extractedText="北京宏图科技有限公司 91110108MA01ABCD23",
        isMatchedWithParties=True,
        complianceNote="公章与甲方名称一致，印迹清晰、无重叠篡改。",
        confidence=0.97,
    )


def _missing_clause() -> s.MissingClause:
    return s.MissingClause(
        id="00000000-0000-0000-0000-000000000050",
        standardClauseId="std-dispute-001",
        title="争议解决条款",
        reason="未约定争议解决方式（诉讼/仲裁）及管辖法院/仲裁机构。",
        suggestion="补充：“本合同争议提交合同签订地有管辖权的人民法院诉讼解决。”",
        canAutoFill=True,
    )


def _contract(contract_id: str) -> s.Contract:
    now = _now()
    return s.Contract(
        id=contract_id,
        fileName=_FILE_NAME,
        fileKey=_FILE_KEY,
        fileType=s.FileType.docx,
        contractType=s.ContractType.purchase,
        typeConfidence=0.96,
        status=s.ContractStatus.pending_review,
        version=1,
        currentVersionId=_FIXED_VERSION_ID,
        elements=_elements(),
        risks=[_risk()],
        conflicts=[_conflict()],
        seals=[_seal()],
        missingClauses=[_missing_clause()],
        healthScore=_health_score(),
        reportId=None,
        createdAt=now,
        updatedAt=now,
    )


def _contract_summary(contract_id: str) -> s.ContractSummary:
    return s.ContractSummary(
        id=contract_id,
        fileName=_FILE_NAME,
        contractType=s.ContractType.purchase,
        status=s.ContractStatus.pending_review,
        healthScore=_health_score(),
        riskCount=1,
        createdAt=_now(),
    )


def _system_audit_configuration(rule_id: str) -> s.SystemAuditConfiguration:
    return s.SystemAuditConfiguration(
        id=rule_id,
        contractType=s.ContractType.purchase,
        promptConfig=s.PromptRoleConfig(
            roleName="严苛法务审计员",
            systemPrompt=(
                "你是一名严苛的合同法务审计员，重点识别违约金过高、责任不对等、"
                "管辖不利、缺失必要条款等风险。"
            ),
            temperature=0.2,
            riskToleranceLevel=s.RiskTolerance.strict,
        ),
        enabledRules=[
            "penalty_too_high",
            "unlimited_liability",
            "missing_dispute_clause",
        ],
        enabled=True,
        reviewThresholds=s.ReviewThresholds(high=0.85, medium=0.65, low=0.40),
        customEntities=[
            s.DynamicExtractionEntityConfig(
                entityKey="guaranteePeriod",
                entityName="质保期",
                description="供应商对货物质量承担保修责任的期限（月）。",
                isRequired=True,
            ),
        ],
        updatedAt=_now(),
        version=1,
    )


class MockDataFactory:
    """Walking Skeleton 集中式 Mock 数据工厂。

    所有 public 方法（stream_task 除外）均为 `@staticmethod`，返回
    `app.generated.schemas` 中的包络 wrapper 实例（继承 ApiEnvelopeBase：
    {code, traceId, serverTime, data}，无 message 字段）。
    路径参数会被动态回显到响应 data 内的主标识字段：
      - get_contract -> data.id = contract_id
      - get_task     -> data.taskId = task_id
      - get_parse_status -> data.taskId = contract_id
    """

    # ============================== Contracts ==============================
    @staticmethod
    def list_contracts(page: int, page_size: int) -> s.ContractListResponse:
        """合同列表（工作台历史，含 ApiEnvelopeBase 包络字段）。"""
        total = 32
        has_more = page * page_size < total
        return s.ContractListResponse(
            code=s.Code.success,
            traceId=_trace_id(),
            serverTime=_now(),
            data=s.Data(
                items=[_contract_summary(_FIXED_CONTRACT_ID)],
                total=total,
                page=page,
                pageSize=page_size,
                hasMore=has_more,
            ),
        )

    @staticmethod
    def presign_upload() -> s.PresignResponse:
        """获取直传凭证（上传三步法 步骤1）。"""
        return _envelope(
            s.PresignResponse,
            s.PresignData(
                presignedUrl=(
                    "https://minio.contract-guard.local/"
                    f"{_FILE_KEY}?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=600"
                ),
                fileKey=_FILE_KEY,
                expiresIn=600,
            ),
        )

    @staticmethod
    def upload_contract(body: s.UploadBody) -> s.ContractUploadResponse:
        """触发解析任务（status=PENDING 语义）。

        TODO: openapi.yaml 的 Status 枚举仅含 RUNNING/DONE/FAILED，缺 PENDING
              成员；“已入队未处理”状态暂用 RUNNING@0.0 近似表达。
        """
        return _envelope(
            s.ContractUploadResponse,
            s.TaskProgress(
                taskId=_FIXED_TASK_ID,
                stage="queued",
                percent=0.0,
                status=s.Status.running,
                result=None,
            ),
        )

    @staticmethod
    def get_contract(contract_id: UUID) -> s.ContractDetailResponse:
        """合同详情（data.id 回显 contract_id）。"""
        return _envelope(s.ContractDetailResponse, _contract(str(contract_id)))

    @staticmethod
    def get_parse_status(contract_id: UUID) -> s.ParseStatusResponse:
        """解析状态轮询（status=DONE, percent=1.0；data.taskId 回显 contract_id）。"""
        return _envelope(
            s.ParseStatusResponse,
            s.TaskProgress(
                taskId=str(contract_id),
                stage="done",
                percent=1.0,
                status=s.Status.done,
                result={"contractId": str(contract_id), "version": 1},
            ),
        )

    @staticmethod
    def get_elements(contract_id: UUID) -> s.ElementListResponse:
        """要素抽取（数据归属于传入 contract_id）。"""
        return _envelope(s.ElementListResponse, _elements())

    @staticmethod
    def get_risks(contract_id: UUID, level: str | None = None) -> s.RiskListResponse:
        """风险列表（按 level 可选过滤）。"""
        risks = [_risk()]
        if level is not None:
            level_norm = level.upper()
            risks = [r for r in risks if r.level.value == level_norm]
        return _envelope(s.RiskListResponse, risks)

    @staticmethod
    def get_conflicts(contract_id: UUID) -> s.ConflictListResponse:
        """跨条款矛盾。"""
        return _envelope(s.ConflictListResponse, [_conflict()])

    @staticmethod
    def get_seals(contract_id: UUID) -> s.SealListResponse:
        """印章签名合规。"""
        return _envelope(s.SealListResponse, [_seal()])

    @staticmethod
    def commit_contract(contract_id: UUID, body: s.CommitBody) -> s.CommitResultResponse:
        """采纳修改 / 新版本（乐观锁；局部失败范式）。"""
        accepted = [it.id for it in body.items if it.accept]
        rejected = [
            s.FailedItem(id=it.id, reason="用户未采纳该修改项。")
            for it in body.items
            if not it.accept
        ]
        return _envelope(
            s.CommitResultResponse,
            s.BatchResult(successIds=accepted, failedItems=rejected),
        )

    # ================================ Tasks ================================
    @staticmethod
    def get_task(task_id: UUID) -> s.TaskProgressResponse:
        """任务进度（status=RUNNING；data.taskId 回显）。"""
        return _envelope(
            s.TaskProgressResponse,
            s.TaskProgress(
                taskId=str(task_id),
                stage="processing",
                percent=0.6,
                status=s.Status.running,
                result=None,
            ),
        )

    @classmethod
    async def stream_task(cls, task_id: UUID) -> AsyncGenerator[s.TaskProgress, None]:
        """SSE 任务进度流（非 staticmethod；逐步产出 TaskProgress 事件）。

        注意：本方法为 async 生成器，用于 Server-Sent Events 流式推送，
        豁免统一包络（直接产出裸 TaskProgress），故未声明为 @staticmethod
        （与规范“stream_task 除外”一致）。
        """
        base = cls.get_task(task_id).data
        for percent, stage, status in [
            (0.2, "queued", s.Status.running),
            (0.6, "processing", s.Status.running),
            (1.0, "done", s.Status.done),
        ]:
            yield base.model_copy(
                update={"percent": percent, "stage": stage, "status": status}
            )

    # ============================ Config / Rules ===========================
    @staticmethod
    def list_rules(contract_type: str | None = None) -> s.SettingsRulesResponse:
        """审计规则列表（按 contract_type 可选过滤）。"""
        rule = _system_audit_configuration(_FIXED_RULE_ID)
        rules = [rule]
        if contract_type is not None and contract_type.upper() != rule.contract_type.value:
            rules = []
        return _envelope(s.SettingsRulesResponse, rules)

    @staticmethod
    def get_rule(config_id: str) -> s.SettingsRuleResponse:
        """单条审计规则（data.id 回显 config_id）。"""
        return _envelope(s.SettingsRuleResponse, _system_audit_configuration(config_id))

    @staticmethod
    def update_rule(config_id: str, body: dict) -> s.SettingsRuleResponse:
        """更新审计规则（局部字段更新 + 版本递增）。"""
        rule = _system_audit_configuration(config_id)
        update_fields: dict[str, Any] = {}
        if "enabledRules" in body:
            update_fields["enabled_rules"] = body["enabledRules"]
        if "riskToleranceLevel" in body:
            update_fields["prompt_config"] = s.PromptRoleConfig(
                roleName=rule.prompt_config.role_name,
                systemPrompt=rule.prompt_config.system_prompt,
                temperature=rule.prompt_config.temperature,
                riskToleranceLevel=s.RiskTolerance(body["riskToleranceLevel"]),
            )
        update_fields["version"] = rule.version + 1
        update_fields["updated_at"] = _now()
        return _envelope(s.SettingsRuleResponse, rule.model_copy(update=update_fields))

    # =============================== Report ================================
    @staticmethod
    def generate_report(contract_id: UUID, body: dict) -> s.ReportGenerateResponse:
        """生成报告任务（status=PENDING 语义）。

        TODO: openapi.yaml 的 Status 枚举缺 PENDING 成员，queued 状态
              暂用 RUNNING@0.0 近似表达。
        """
        return _envelope(
            s.ReportGenerateResponse,
            s.TaskProgress(
                taskId=_FIXED_TASK_ID,
                stage="queued",
                percent=0.0,
                status=s.Status.running,
                result={"contractId": str(contract_id)},
            ),
        )

    @staticmethod
    def get_report(contract_id: UUID) -> s.ReportResponse:
        """获取报告元信息（data.contractId 回显；含 MinIO 预签名下载 URL）。"""
        return _envelope(
            s.ReportResponse,
            s.ReportData(
                reportId=_FIXED_REPORT_ID,
                contractId=str(contract_id),
                status=s.Status.done,
                downloadUrl=(
                    f"https://minio.contract-guard.local/reports/"
                    f"{contract_id}/audit-report.pdf"
                    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=600"
                ),
                generatedAt=_now(),
            ),
        )

    # ============================== Feedback ===============================
    @staticmethod
    def create_feedback(body: s.FeedbackCreateBody) -> s.FeedbackResponse:
        """创建反馈（HTTP 201）。"""
        return _envelope(
            s.FeedbackResponse,
            s.FeedbackItem(
                id=_FIXED_FEEDBACK_ID,
                contractId=body.contract_id,
                targetType=body.target_type,
                targetId=body.target_id,
                verdict=body.verdict,
                userCorrection=body.user_correction,
                reviewer=body.reviewer,
                isIncorporated=False,
                createdAt=_now(),
            ),
        )

    # =============================== Agent =================================
    @staticmethod
    def rewrite_clause(body: s.AgentRewriteRequest) -> s.RewriteResponse:
        """条款改写（CoT + RAG 溯源）。"""
        before = "乙方应在收到货物后 3 日内付款，逾期按合同总额 30% 支付违约金。"
        after = (
            "乙方应在收到货物后 15 个工作日内支付首期款项，逾期按未付款项的 5% "
            "支付违约金，且违约金总额不超过未付款项的 30%。"
        )
        return _envelope(
            s.RewriteResponse,
            s.RewriteData(
                taskId=_FIXED_TASK_ID,
                clauseId=body.clause_id,
                before=before,
                after=after,
                diff=s.DiffMessage(
                    type="DIFF",
                    before=before,
                    after=after,
                    clauseId=body.clause_id,
                ),
                reasoningSteps=_reasoning_steps(),
                citations=[_citation()],
                updatedAt=_now(),
            ),
        )
