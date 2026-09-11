"""cg-observability 自检（设计文档 T01 验收标准 4）。

覆盖：
1. `log_event()` 输出**单行** JSON 且带 `trace_id`
2. trace_id 通过 contextvars 透传（跨函数、跨 Span）
3. Token 计数：估算兜底口径 + 精确/估算标记
4. TokenMeter 的累加与分段耗时统计
"""

from __future__ import annotations

import io
import json
import logging
import time
from contextvars import copy_context
from typing import Any

from cg_observability import (
    Span,
    TokenCounter,
    TokenMeter,
    TraceContext,
    get_logger,
    get_trace_id,
    log_event,
    new_trace_id,
)


def test_log_event_emits_single_line_json_with_trace_id() -> None:
    """验收标准：输出为单行 JSON 且带 trace_id。"""
    logger = get_logger("test.observability")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logger.handlers[0].formatter)
    logger.addHandler(handler)
    try:
        with TraceContext(name="unit") as ctx:
            log_event(
                logger,
                "retrieve.done",
                trace_id=ctx.trace_id,
                candidates=40,
                top_score=0.87,
                token_count=1532,
            )
        raw = buf.getvalue().strip()
    finally:
        logger.removeHandler(handler)

    assert raw.count("\n") == 0, "日志必须是单行（便于 Filebeat/Loki 采集）"
    payload = json.loads(raw)  # 必须是合法 JSON
    assert payload["stage"] == "retrieve.done"
    assert payload["trace_id"], "trace_id 必须存在且非空"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.observability"
    assert payload["msg"] == "retrieve.done"
    # 业务字段必须出现在顶层
    assert payload["candidates"] == 40
    assert payload["top_score"] == 0.87
    assert payload["token_count"] == 1532
    # 固定字段
    assert set(["ts", "level", "logger", "stage", "trace_id", "span_id", "msg"]) <= set(payload)


def test_chinese_is_not_escaped_and_newline_is_escaped() -> None:
    """ensure_ascii=False 保留中文；正文换行必须被转义以保证单行。"""
    logger = get_logger("test.observability.encoding")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logger.handlers[0].formatter)
    logger.addHandler(handler)
    try:
        log_event(logger, "chunk.done", msg="切片完成\n第二行", 标题="违约金")
    finally:
        logger.removeHandler(handler)

    raw = buf.getvalue().strip()
    assert raw.count("\n") == 0
    payload = json.loads(raw)
    assert payload["msg"] == "切片完成\n第二行"
    assert payload["标题"] == "违约金"
    assert "违约金" in raw, "中文应可读而非被转义为 \\uXXXX"


def test_trace_id_propagates_via_contextvars() -> None:
    """trace_id 必须能从外层函数透传到内层（无需逐层传参）。"""

    def inner() -> str:
        return get_trace_id()

    with TraceContext(trace_id="trace-abc") as ctx:
        assert ctx.trace_id == "trace-abc"
        assert inner() == "trace-abc"
        with ctx.span("dense") as sp:
            assert sp.trace_id == "trace-abc"
            assert inner() == "trace-abc"
    # 退出上下文后应还原
    assert get_trace_id() != "trace-abc"


def test_trace_id_survives_copy_context() -> None:
    """线程池场景：`copy_context().run()` 必须能带走 trace_id。"""
    with TraceContext(trace_id="trace-xyz"):
        ctx = copy_context()

        def read() -> str:
            return get_trace_id()

        assert ctx.run(read) == "trace-xyz"


def test_trace_context_auto_generates_trace_id() -> None:
    """未显式指定 trace_id 时自动生成 UUID4。"""
    with TraceContext() as ctx:
        assert len(ctx.trace_id) == 36
        assert new_trace_id() != ctx.trace_id


def test_span_measures_elapsed() -> None:
    """Span 必须能计时并记录属性。"""
    with Span("retrieve.dense") as sp:
        time.sleep(0.02)
        sp.set(candidates=20)
    assert sp.elapsed_ms >= 15, f"耗时统计异常：{sp.elapsed_ms}ms"
    assert sp.attributes["candidates"] == 20
    assert sp.to_dict()["span"] == "retrieve.dense"


def test_trace_context_collects_spans_and_elapsed() -> None:
    """TraceContext 汇总各 span 与总耗时。"""
    with TraceContext(name="smoke") as ctx:
        with ctx.span("embed"):
            time.sleep(0.01)
        with ctx.span("dense"):
            time.sleep(0.01)
    snap = ctx.to_dict()
    assert len(snap["spans"]) == 2
    assert [s["span"] for s in snap["spans"]] == ["embed", "dense"]
    assert snap["elapsed_ms"] >= 15


def test_token_counter_estimate_fallback() -> None:
    """无 tiktoken 时按 ceil(len/1.5) 估算，并标记 estimated=True。"""
    counter = TokenCounter(force_estimate=True)
    assert counter.backend == "estimate"
    assert counter.estimated is True
    assert counter.count("") == 0
    assert counter.count("违约金") == 2  # ceil(3 / 1.5) = 2
    assert counter.count("a" * 15) == 10  # ceil(15 / 1.5) = 10
    assert counter.count_many(["违约金", "付款期限"]) == 2 + 3
    assert counter.count_batch(["ab", "abcd"]) == [2, 3]


def test_token_counter_tiktoken_when_available() -> None:
    """有 tiktoken 时走精确计数（未安装则自动回落到估算，均视为通过）。"""
    counter = TokenCounter()
    n = counter.count("hello world")
    assert n > 0
    assert counter.backend in {"tiktoken", "estimate"}
    assert counter.estimated == (counter.backend == "estimate")


def test_token_meter_accumulates_and_times() -> None:
    """Token 累加 + 分段耗时统计（喂给 token_usage / stage_latency_ms）。"""
    meter = TokenMeter(counter=TokenCounter(force_estimate=True))
    with meter.stage("embed"):
        time.sleep(0.02)
        meter.count_text("embedding", "违约金按日千分之五计算")
    with meter.stage("dense"):
        time.sleep(0.01)
    meter.add("prompt", 120)

    assert meter.usage("embedding") == meter.counter.count("违约金按日千分之五计算")
    assert meter.total == meter.usage("embedding") + 120
    assert meter.latency_ms("embed") >= 15
    assert meter.latency_ms("dense") >= 5
    assert set(meter.latencies) == {"embed", "dense"}

    snap = meter.snapshot()
    assert snap["total_tokens"] == meter.total
    assert snap["estimated"] is True
    assert snap["backend"] == "estimate"


def test_token_meter_ignores_non_positive() -> None:
    """负数与 0 不应污染累加结果。"""
    meter = TokenMeter()
    meter.add("prompt", 10)
    meter.add("prompt", 0)
    meter.add("prompt", -5)
    assert meter.usage("prompt") == 10


def test_token_meter_record_latency_manual() -> None:
    """手工登记耗时的能力（无法用 with 包裹的场景）。"""
    meter = TokenMeter()
    meter.record_latency("rerank", 42)
    meter.record_latency("rerank", 8)
    assert meter.latency_ms("rerank") == 50


def test_logger_is_idempotent() -> None:
    """重复 get_logger 不应重复挂 handler（否则日志会翻倍）。"""
    a = get_logger("test.observability.idempotent")
    n = len(a.handlers)
    b = get_logger("test.observability.idempotent")
    assert a is b
    assert len(b.handlers) == n


def test_log_event_level_mapping() -> None:
    """级别字符串必须正确映射，且未知级别回落 INFO。"""
    logger = get_logger("test.observability.levels")
    records: list[Any] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    cap = _Capture()
    logger.addHandler(cap)
    try:
        log_event(logger, "s1", level="warning")
        log_event(logger, "s2", level="ERROR")
        log_event(logger, "s3", level="NOT_A_LEVEL")
    finally:
        logger.removeHandler(cap)

    assert [r.levelname for r in records] == ["WARNING", "ERROR", "INFO"]
