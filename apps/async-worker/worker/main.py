"""Worker 启动入口（Celery 单一实例）。

任务模块经 `include` 注册；基础设施地址经环境变量注入（与 api-gateway 同源约定）。
"""

from __future__ import annotations

import os

from celery import Celery

BROKER_URL = os.getenv("CG_CELERY_BROKER", "redis://localhost:6379/1")
BACKEND_URL = os.getenv("CG_CELERY_BACKEND", "redis://localhost:6379/2")

celery_app = Celery(
    "contract_guard_worker",
    broker=BROKER_URL,
    backend=BACKEND_URL,
    include=[
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
)

# 便于 `uv run --package async-worker celery -A worker.main worker`
app = celery_app

if __name__ == "__main__":
    celery_app.worker_main(argv=["worker", "--loglevel=info"])
