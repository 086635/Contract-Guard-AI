"""解析任务接线自检（T-C7 / `3-6 §⑧`）。

用 `task.apply()` **同步执行**（不需要 broker）；
进度上报用桩任务验证（单调不回退、未知阶段忽略、无 broker 时不抛）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from worker.progress import PARSE_STAGE_PERCENT, PARSE_STAGES, ParseProgressReporter
from worker.tasks.parse_doc import parse_doc_task

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "packages" / "doc-processor" / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _disable_result_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 `update_state` **立即失败**而不是去连 redis。

    没有结果后端时，Celery 会按重试策略反复尝试连接（实测单次任务因此多花 ~30 s）；
    这里把 backend 置空，`update_state` 会秒失败 —— 正好也覆盖「上报失败必须降级」这条契约。
    """
    monkeypatch.setattr(parse_doc_task.app.conf, "result_backend", None, raising=False)


def _run(file_key: str, contract_id: str, local_path: Path | str | None = None) -> dict:
    """同步跑任务（`local_path` 省略时不传该 kwarg）。"""
    kwargs = {} if local_path is None else {"local_path": str(local_path)}
    return parse_doc_task.apply(args=(file_key, contract_id), kwargs=kwargs).get()


def test_task_apply_pdf_native() -> None:
    """数字版 PDF 经任务跑通：四类产出齐全、阶段日志完整。"""
    result = _run("ignored-key", "c-apply", _FIXTURES / "native.pdf")
    # 印章模块（3-8）已落盘、夹具无红章 → 全链路 SUCCESS
    assert result["status"] == "SUCCESS"
    assert result["fileType"] == "PDF_NATIVE"
    assert result["sealDetections"] == []
    assert result["clauses"] and result["layoutBlocks"]
    assert result["artifactPath"]
    assert {entry["stage"] for entry in result["stageLog"]} >= {
        "route",
        "parse_text",
        "seal",
        "extract",
        "index",
    }


def test_task_apply_docx() -> None:
    """DOCX：合成坐标 + 所有条款 bbox 为 None。"""
    result = _run("ignored-key", "c-docx-task", _FIXTURES / "contract.docx")
    assert result["fileType"] == "DOCX"
    assert result["coordinatesSynthetic"] is True
    assert result["clauses"]
    assert all(clause["bbox"] is None for clause in result["clauses"])


def test_task_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """源文件拿不到 → 返回 FAILED 信封（**不抛异常给 broker**）。"""

    class _Broken:
        def fget_object(self, bucket: str, key: str, path: str) -> None:
            raise OSError("minio down")

    monkeypatch.setattr("worker.storage.get_minio_client", lambda: _Broken(), raising=False)
    result = _run("no-such-file.pdf", "c-missing")
    assert result["status"] == "FAILED"
    assert any("SOURCE_UNAVAILABLE" in item for item in result["warnings"])


def test_task_signature_stable() -> None:
    """签名稳定：前两个参数必须是 `file_key` / `contract_id`（参数顺序即跨进程约定）。

    注：`bind=True` 的 `self` 已被 Celery 包装掉，`run` 上看到的是纯业务参数。
    """
    parameters = list(inspect.signature(parse_doc_task.run).parameters)
    assert parameters[:2] == ["file_key", "contract_id"]
    assert "local_path" in parameters


def test_stage_log_shape() -> None:
    """`stageLog` 每条都含四键（与 T-B2 的 `StageLog` 同形）。"""
    result = _run("ignored-key", "c-stage", _FIXTURES / "native.pdf")
    assert result["stageLog"]
    for entry in result["stageLog"]:
        assert set(entry) == {"stage", "status", "latency_ms", "detail"}


def test_progress_percent_monotonic() -> None:
    """进度单调不减、未知阶段被忽略、以 1.0 收尾。"""

    class _Task:
        def __init__(self) -> None:
            self.events: list[tuple[str, float, str]] = []

        def update_state(self, state: str | None = None, meta: dict | None = None) -> None:
            assert meta is not None
            self.events.append((state or "", meta["percent"], meta["stage"]))

    task = _Task()
    reporter = ParseProgressReporter(task, "c-1", "trace-1")
    reporter.start()
    reporter.stage("route")
    reporter.stage("not-a-stage")  # 非法阶段：只告警，不改变百分比
    reporter.stage("index")
    percentages = [percent for _, percent, _ in task.events]
    assert percentages == sorted(percentages)
    assert reporter.percent == pytest.approx(PARSE_STAGE_PERCENT["index"])

    reporter.finish({"status": "SUCCESS"})
    assert task.events[-1][1] == 1.0
    assert all(name == "start" or name in PARSE_STAGES or name == "done" for _, _, name in task.events)


def test_progress_failure_does_not_raise() -> None:
    """`update_state` 炸了（无 broker）也只告警，不影响任务本身。"""

    class _Broken:
        def update_state(self, state: str | None = None, meta: dict | None = None) -> None:
            raise RuntimeError("no broker")

    reporter = ParseProgressReporter(_Broken(), "c-1", "trace-1")
    reporter.start()
    reporter.stage("route")
    assert reporter.percent > 0


def test_agents_md_env_prefix() -> None:
    r"""环境前缀自证：`AGENTS.md §4.1` 必须含 `packages\doc-processor`（否则模块 3 断言不可执行）。"""
    text = (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "packages\\doc-processor" in text
