"""DOCX→PDF 转换任务（C59 / `4-07 §⑥.6`）。

流程：把源 DOCX 落到**本任务私有**临时目录 → LibreOffice headless 转 PDF
→ 派生 PDF 上传 MinIO（服务端生成 key）→ 返回结构化结果。

🔴 红线：每任务独立临时目录（§⑨-1）；转换失败必须返回结构化 `FAILED`、
不抛异常给 broker（§⑨-2）；原文只读，不删除也不就地覆盖（§② 不做 / 断言 4）。
🔴 模块边界（`4-07 §⑥.6`）：worker **不写业务 DB**；`converterVersion` 只随结果回传，
由网关侧 `PreviewService` 回写 `contract_previews`。

对应：`文件集合/codex/4.C59DOCX转PDF任务与Convert队列.md`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from cg_observability import get_logger, log_event
from worker.main import celery_app
from worker.storage import get_minio_client

_LOG = get_logger("worker.convert")

#: 结果状态（平台任务词表；网关 `TaskService` 依 `FAILED` 判失败）。
SUCCESS = "SUCCESS"
FAILED = "FAILED"

#: 派生预览 PDF 的对象键前缀（与 C58 `contract_previews` 表同名前缀）。
PREVIEW_KEY_PREFIX = "contract_previews"

#: 转换超时（秒）：可用 `CG_CONVERT_TIMEOUT_S` 覆盖。
DEFAULT_TIMEOUT_S = 120.0

_soffice_version_cache: str | None = None


class ConvertFailedError(RuntimeError):
    """转换失败的安全摘要异常（消息只含错误码，不含文件内容）。"""


def _soffice_binary() -> str:
    """LibreOffice 可执行文件名（compose 内为 `soffice`）。"""
    return os.getenv("CG_SOFFICE_BIN", "soffice")


def _timeout_s() -> float:
    """读取转换超时。"""
    return float(os.getenv("CG_CONVERT_TIMEOUT_S", str(DEFAULT_TIMEOUT_S)))


def converter_version() -> str:
    """返回转换器版本（一次性探测并缓存）。

    可用 `CG_CONVERTER_VERSION` 显式固定（容器构建时写入最稳）；
    否则执行 `soffice --version`；不可用时给安全占位，绝不抛异常给任务。
    """
    global _soffice_version_cache
    pinned = os.getenv("CG_CONVERTER_VERSION", "").strip()
    if pinned:
        return pinned
    if _soffice_version_cache is None:
        _soffice_version_cache = _probe_soffice_version()
    return _soffice_version_cache


def _probe_soffice_version() -> str:
    try:
        completed = subprocess.run(
            [_soffice_binary(), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "libreoffice-unavailable"
    first_line = (completed.stdout or completed.stderr or "").strip().splitlines()
    return first_line[0].strip() if first_line else "libreoffice-unknown"


def convert_document(source: Path, out_dir: Path, *, timeout_s: float) -> Path:
    """在 `out_dir` 内用 LibreOffice headless 把 `source` 转成 PDF。

    Args:
        source: 已落到任务私有目录的源文件（只读输入）。
        out_dir: 本次任务独占的输出目录。
        timeout_s: 子进程超时；到点抛 `subprocess.TimeoutExpired`。

    Returns:
        产物 PDF 路径。

    Raises:
        ConvertFailedError: 非 0 退出或未产出非空 PDF。
        subprocess.TimeoutExpired: 超时（由调用方折算成 FAILED）。
    """
    completed = subprocess.run(
        [
            _soffice_binary(),
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out_dir),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    if completed.returncode != 0:
        raise ConvertFailedError(f"CONVERT_EXIT_CODE:{completed.returncode}")
    produced = out_dir / f"{source.stem}.pdf"
    if not produced.is_file() or produced.stat().st_size == 0:
        raise ConvertFailedError("CONVERT_NO_OUTPUT")
    return produced


def preview_object_key(contract_id: str, out_name: str) -> str:
    """服务端生成的派生 PDF 对象键（每次转换唯一 → 重试不覆盖任何对象）。"""
    return f"{PREVIEW_KEY_PREFIX}/{contract_id}/{uuid4().hex}-{out_name}"


def _stage_source(source_key: str, local_path: str | None, work_dir: Path) -> Path:
    """把源 DOCX 落到本任务私有目录；原文对象只读，不就地转换。"""
    if local_path:
        candidate = Path(local_path)
        if not candidate.is_file():
            raise FileNotFoundError(local_path)
        target = work_dir / f"source{candidate.suffix.lower() or '.docx'}"
        shutil.copyfile(candidate, target)
        return target

    suffix = Path(source_key).suffix.lower() or ".docx"
    target = work_dir / f"source{suffix}"
    bucket = os.getenv("CG_MINIO_BUCKET", "contracts")
    get_minio_client().fget_object(bucket, source_key, str(target))
    return target


def _upload_pdf(object_key: str, pdf: Path) -> None:
    """上传派生 PDF（不是覆盖原文对象）。"""
    bucket = os.getenv("CG_MINIO_BUCKET", "contracts")
    size = pdf.stat().st_size
    with pdf.open("rb") as handle:
        get_minio_client().put_object(
            bucket,
            object_key,
            handle,
            length=size,
            content_type="application/pdf",
        )


def _failure(
    contract_id: str,
    reason: str,
    *,
    trace_id: str,
    source_key: str,
) -> dict[str, object]:
    """构造结构化 FAILED 结果（只含安全摘要）。"""
    log_event(
        _LOG,
        "convert.fail",
        level="WARNING",
        trace_id=trace_id,
        contract_id=contract_id,
        source_key=source_key,
        reason=reason,
    )
    return {
        "contractId": contract_id,
        "status": FAILED,
        "storagePath": None,
        "converterVersion": None,
        "warnings": [reason],
    }


@celery_app.task(name="convert_doc")
def convert_doc_task(
    contract_id: str,
    source_key: str,
    *,
    local_path: str | None = None,
) -> dict:
    """把 DOCX 转成预览 PDF（C59 §⑤）。

    Args:
        contract_id: 合同 ID（进结果信封，并作为派生对象键的命名空间）。
        source_key: 源 DOCX 的 MinIO 对象键（`local_path` 给定时仅作占位）。
        local_path: 离线联调/测试用的本地路径（与 `parse_doc` 同款）。

    Returns:
        `{contractId, status, storagePath, converterVersion, warnings}`；
        `status ∈ {SUCCESS, FAILED}`。**任何情况下都不抛异常给 broker。**
    """
    trace_id = uuid4().hex
    work_dir = Path(tempfile.mkdtemp(prefix=f"cg-convert-{contract_id}-"))
    try:
        source = _stage_source(source_key, local_path, work_dir)
        pdf = convert_document(source, work_dir, timeout_s=_timeout_s())
        object_key = preview_object_key(contract_id, pdf.name)
        _upload_pdf(object_key, pdf)
    except subprocess.TimeoutExpired:
        return _failure(
            contract_id,
            "CONVERT_TIMEOUT",
            trace_id=trace_id,
            source_key=source_key,
        )
    except ConvertFailedError as exc:
        return _failure(
            contract_id,
            str(exc),
            trace_id=trace_id,
            source_key=source_key,
        )
    except Exception as exc:  # noqa: BLE001 - 拿不到源/上传失败等一律结构化 FAILED
        return _failure(
            contract_id,
            f"CONVERT_UNAVAILABLE:{type(exc).__name__}",
            trace_id=trace_id,
            source_key=source_key,
        )
    finally:
        # 每任务独占目录：无论成败都清理，绝不共享给下一次转换。
        shutil.rmtree(work_dir, ignore_errors=True)

    version = converter_version()
    log_event(
        _LOG,
        "convert.success",
        trace_id=trace_id,
        contract_id=contract_id,
        source_key=source_key,
        object_key=object_key,
        converter_version=version,
    )
    return {
        "contractId": contract_id,
        "status": SUCCESS,
        "storagePath": object_key,
        "converterVersion": version,
        "warnings": [],
    }
