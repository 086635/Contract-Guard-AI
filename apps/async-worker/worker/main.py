"""Worker 启动入口（Celery 单一实例）。

任务模块经 `include` 注册；基础设施地址经环境变量注入（与 api-gateway 同源约定）。
"""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import worker_process_init


@worker_process_init.connect
def initialize_image_runtime(**kwargs: object) -> None:
    """Windows native image routines run inside the worker's existing task pool."""
    if os.name == "nt":
        try:
            import cv2
        except ImportError:
            return  # The existing OCR availability check reports a missing engine.
        cv2.setNumThreads(1)

BROKER_URL = os.getenv("CG_CELERY_BROKER", "redis://localhost:6379/1")
BACKEND_URL = os.getenv("CG_CELERY_BACKEND", "redis://localhost:6379/2")
#: C59：DOCX→PDF 预览转换的独立队列（专用 worker 用 `-Q convert` 消费）。
CONVERT_QUEUE = os.getenv("CG_CELERY_CONVERT_QUEUE", "convert")

# ---- C62 任务可靠性参数（可经环境变量覆盖；软超时必须小于硬超时）----
#: 软超时：到点抛 `SoftTimeLimitExceeded`，任务可观察/收尾（默认 10 分钟）。
TASK_SOFT_TIME_LIMIT = int(os.getenv("CG_TASK_SOFT_TIME_LIMIT", "600"))
#: 硬超时：到点直接杀 worker 子进程（默认 15 分钟，必须 > 软超时）。
TASK_TIME_LIMIT = int(os.getenv("CG_TASK_TIME_LIMIT", "900"))
#: 单任务最大重试次数（**有限重试**：禁止无限重试）。
TASK_MAX_RETRIES = int(os.getenv("CG_TASK_MAX_RETRIES", "3"))
#: 退避上限（秒）；基数用 Celery `retry_backoff` 的默认 1s 起步，配合 jitter 抖动。
TASK_RETRY_BACKOFF_MAX_S = float(os.getenv("CG_TASK_RETRY_BACKOFF_MAX_S", "300"))
#: 只对**瞬时/基础设施类**异常自动重试（分类重试：不吞编程错误）。
RETRYABLE_EXCEPTIONS = (OSError, TimeoutError)

#: 可重试任务白名单（⑥-2：重试只对**幂等且可重放**的任务开放）。
RETRYABLE_TASKS = ("parse_doc", "scan_upload", "convert_doc")


def _retry_annotation() -> dict[str, object]:
    """构造「有限 + 指数退避 + 抖动」的重试注解（唯一定义处）。"""
    return {
        "autoretry_for": RETRYABLE_EXCEPTIONS,
        "max_retries": TASK_MAX_RETRIES,
        "retry_backoff": True,
        "retry_backoff_max": TASK_RETRY_BACKOFF_MAX_S,
        "retry_jitter": True,
    }

celery_app = Celery(
    "contract_guard_worker",
    broker=BROKER_URL,
    backend=BACKEND_URL,
    include=[
        "worker.tasks.scan_upload",
        "worker.tasks.convert_doc",
        "worker.tasks.parse_doc",
        "worker.tasks.run_audit",
        "worker.tasks.gen_report",
        "worker.tasks.rewrite",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,
    # C59：转换任务只进 convert 队列，避免与解析/审核任务互相拖慢。
    task_routes={"convert_doc": {"queue": CONVERT_QUEUE}},
    # ---- C62：可靠性策略（DB 任务行是状态源，Celery 只负责执行与重投）----
    # 先执行后确认：worker 崩溃时消息重回队列，不会静默丢任务。
    task_acks_late=True,
    # worker 被杀（OOM/kill -9）时也重投，而不是留下永久 RUNNING。
    task_reject_on_worker_lost=True,
    # 软/硬超时：软超时可捕获收尾，硬超时兜底杀死子进程。
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT,
    task_time_limit=TASK_TIME_LIMIT,
    # 默认重试延迟（未开启 backoff 的任务用），并给可重试任务挂上有限退避注解。
    task_default_retry_delay=TASK_RETRY_BACKOFF_MAX_S / max(1, TASK_MAX_RETRIES),
    task_annotations={task_name: _retry_annotation() for task_name in RETRYABLE_TASKS},
)

# 便于 `uv run --package async-worker celery -A worker.main worker`
app = celery_app

if __name__ == "__main__":
    celery_app.worker_main(argv=["worker", "--loglevel=info"])
