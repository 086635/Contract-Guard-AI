"""文档解析与 OCR 任务（T-C7 / `3-6`，§10 步骤 2）。

流程：定位源文件（本地 / MinIO）→ 分流 → 四路取文本 → 印章 → 版面 → 要素/条款 → 偏移索引
→ 结果信封 + `data/parse/{contractId}.json` 工件。

🔴 **不写数据库**：本仓当前没有任何 DB DAL（`api-gateway` 仍是 mock 驱动），
"落库 + `Contract.status = PENDING_REVIEW`" 属待建任务（`3-6 §⑨-8`），不在本任务内假装完成。
"""

from __future__ import annotations

import uuid

from cg_observability import get_logger, log_event
from worker.main import celery_app
from worker.parse_pipeline import (
    ParseOutcome,
    SourceUnavailableError,
    build_parse_pipeline,
    resolve_source,
)
from worker.progress import ParseProgressReporter

_LOG = get_logger("worker.parse")


@celery_app.task(name="parse_doc", bind=True)
def parse_doc_task(
    self,
    file_key: str,
    contract_id: str,
    *,
    local_path: str | None = None,
) -> dict:
    """解析单个合同文档（§10 步骤 2）。

    Args:
        file_key: MinIO 对象键或本地路径。
        contract_id: 合同 ID（进信封与工件名）。
        local_path: 联调/测试用的本地路径（优先级最高）。

    Returns:
        `ParseOutcome.to_payload()`（字段表见 `3-6 §5.3`）；
        `status ∈ {SUCCESS, DEGRADED, FAILED}`。**任何情况下都不抛异常给 broker。**
    """
    trace_id = uuid.uuid4().hex
    reporter = ParseProgressReporter(self, contract_id, trace_id)
    pipeline = build_parse_pipeline(reporter=reporter)

    try:
        reporter.start()
        source = resolve_source(file_key, local_path)
        pipeline.report_fetch()
        log_event(
            _LOG,
            "parse.start",
            trace_id=trace_id,
            contract_id=contract_id,
            file_key=file_key,
            source=str(source),
        )
        outcome = pipeline.run(source, contract_id)
    except SourceUnavailableError as exc:
        # 源文件拿不到：没有输入就没有任何产出 → FAILED（不是 DEGRADED）
        outcome = ParseOutcome(
            contract_id=contract_id,
            status="FAILED",
            trace_id=trace_id,
            warnings=[f"SOURCE_UNAVAILABLE：{exc}"],
        )
        pipeline.report_fail("SOURCE_UNAVAILABLE")
        log_event(
            _LOG,
            "parse.fail",
            level="WARNING",
            trace_id=trace_id,
            contract_id=contract_id,
            reason=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - 兜底：连管道都没起来，也必须返回结构化失败
        outcome = ParseOutcome(
            contract_id=contract_id,
            status="FAILED",
            trace_id=trace_id,
            warnings=[f"UNEXPECTED：{type(exc).__name__}：{exc!r}"],
        )
        pipeline.report_fail(f"{type(exc).__name__}")
        log_event(
            _LOG,
            "parse.fail",
            level="ERROR",
            trace_id=trace_id,
            contract_id=contract_id,
            error=repr(exc),
        )

    payload = outcome.to_payload()
    if outcome.status != "FAILED":
        pipeline.report_finish(payload)
    return payload
