"""C62 Celery 可靠性策略测试（离线）。

配置即契约：acks_late / prefetch=1 / 软硬超时 / 有限指数退避 + 抖动，
且**只对幂等且可重放的任务**开自动重试（§⑨-2：分类 + 上限，不无限重试）。
"""

from __future__ import annotations

import subprocess
import sys

from celery import Celery

from worker.main import (
    CONVERT_QUEUE,
    RETRYABLE_EXCEPTIONS,
    RETRYABLE_TASKS,
    TASK_MAX_RETRIES,
    TASK_RETRY_BACKOFF_MAX_S,
    TASK_SOFT_TIME_LIMIT,
    TASK_TIME_LIMIT,
    celery_app,
)


def test_celery_reliability_config() -> None:
    """可靠性配置齐备：acks_late / prefetch / 软硬超时 / 有限退避注解。"""
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_reject_on_worker_lost is True

    assert conf.task_soft_time_limit == TASK_SOFT_TIME_LIMIT
    assert conf.task_time_limit == TASK_TIME_LIMIT
    assert conf.task_soft_time_limit < conf.task_time_limit

    annotations = conf.task_annotations
    assert set(annotations) == set(RETRYABLE_TASKS)
    for task_name, annotation in annotations.items():
        assert annotation["max_retries"] == TASK_MAX_RETRIES, task_name
        assert annotation["retry_backoff"] is True, task_name
        assert annotation["retry_backoff_max"] == TASK_RETRY_BACKOFF_MAX_S, task_name
        assert annotation["retry_jitter"] is True, task_name
        # 分类重试：只对瞬时/基础设施异常重试，不吞编程错误
        assert set(annotation["autoretry_for"]) == set(RETRYABLE_EXCEPTIONS), task_name

    # C59 的 convert 队列路由不能被可靠性配置覆盖
    assert conf.task_routes["convert_doc"]["queue"] == CONVERT_QUEUE == "convert"


def test_acks_late_config() -> None:
    """先执行后确认：worker 崩溃不会静默丢任务。"""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_soft_time_limit() -> None:
    """软超时短于硬超时，且都由环境变量驱动（默认 600/900）。"""
    assert TASK_SOFT_TIME_LIMIT == 600
    assert TASK_TIME_LIMIT == 900
    assert TASK_SOFT_TIME_LIMIT < TASK_TIME_LIMIT
    assert celery_app.conf.task_soft_time_limit == 600
    assert celery_app.conf.task_time_limit == 900


def test_retry_only_for_idempotent_tasks() -> None:
    """重试白名单只含可重放任务；上限为有限值（禁止无限重试）。"""
    assert set(RETRYABLE_TASKS) == {"parse_doc", "scan_upload", "convert_doc"}
    assert 1 <= TASK_MAX_RETRIES <= 10
    assert TASK_RETRY_BACKOFF_MAX_S > 0
    assert all(issubclass(exc, BaseException) for exc in RETRYABLE_EXCEPTIONS)
    assert Exception not in RETRYABLE_EXCEPTIONS  # 不做「任何异常都重试」


def test_reliability_params_are_env_overridable() -> None:
    """参数走环境变量（部署可调），且软超时 < 硬超时。"""
    script = (
        "import os;"
        "os.environ['CG_TASK_SOFT_TIME_LIMIT']='30';"
        "os.environ['CG_TASK_TIME_LIMIT']='45';"
        "os.environ['CG_TASK_MAX_RETRIES']='5';"
        "from worker.main import celery_app;"
        "print(celery_app.conf.task_soft_time_limit, celery_app.conf.task_time_limit,"
        " celery_app.conf.task_annotations['parse_doc']['max_retries'])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "30 45 5"


def test_celery_app_is_single_instance() -> None:
    """复用唯一 Celery 实例（不新增第二套任务框架）。"""
    from worker import main as main_module

    assert isinstance(celery_app, Celery)
    assert main_module.app is celery_app
    assert "worker.tasks.convert_doc" in celery_app.conf.include
    assert "worker.tasks.scan_upload" in celery_app.conf.include
