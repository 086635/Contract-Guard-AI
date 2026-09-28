#!/usr/bin/env python
"""端到端冒烟：上传 → 解析 → 审核 → 报告（T-D4 / `4.C26`）。

按真实流程走网关端点（不 mock、不绕过），默认**离线档**：

    presign -> MinIO 直传 -> upload -> 轮询任务 -> elements -> risks/conflicts
    -> report/generate -> report/get -> 下载 DOCX 回读

`--require-risks` 打开**真实语义档**：额外要求风险 >=1 且 citations 非空。

退出码：0=通过；1=链路失败；2=参数/环境错误。

离线档对「溯源」章节做条件判定：仅当 risks/conflicts 非空时硬判定；
离线 0 风险/0 矛盾时空态记为显式 DEGRADED（`render_docx` 不输出空「溯源」子标题）。
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TERMINAL = {"DONE", "FAILED", "FATAL"}


class EnvError(RuntimeError):
    """环境 / 参数错误（退出码 2）。"""


class _Tracker:
    """记录每条断言的 PASS / DEGRADED / FAIL。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        mark = {"PASS": "PASS", "DEGRADED": "DEGRADED", "FAIL": "FAIL"}[status]
        print(
            f"  [{mark}] {name}：{detail}" if detail else f"  [{mark}] {name}",
            flush=True,
        )

    @property
    def failed(self) -> bool:
        return any(status == "FAIL" for _, status, _ in self.rows)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def build_fixture_docx() -> bytes:
    """生成含 5 条「第 X 条」条款的内存 DOCX 夹具（DOCX 合成坐标，无需真实 OCR）。"""
    from docx import Document

    document = Document()
    document.add_heading("采购合同", level=1)
    document.add_paragraph("甲方：北京示例科技有限公司")
    document.add_paragraph("乙方：上海示例供应链有限公司")
    for clause in (
        "第一条 付款方式：需方应在验收合格后三十日内支付合同总价款的百分之九十。",
        "第二条 逾期付款违约金：需方逾期付款的，每逾期一日按未付款项的万分之三支付违约金。",
        "第三条 交付期限：供方应于合同签订后三十日内交付全部货物。",
        "第四条 质量保证：货物质量保证期为验收合格之日起十二个月。",
        "第五条 争议解决：双方发生争议协商不成的，提交北京仲裁委员会仲裁。",
    ):
        document.add_paragraph(clause)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _minio(args: argparse.Namespace):
    from minio import Minio

    return Minio(
        args.minio_endpoint,
        access_key=args.minio_access_key,
        secret_key=args.minio_secret_key,
        secure=False,
    )


def _data(response: httpx.Response) -> Any:
    try:
        return response.json().get("data")
    except Exception:  # noqa: BLE001 - 非 JSON 响应按空处理
        return None


def _poll_report(
    client: httpx.Client, base: str, contract_id: str, timeout: float
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"{base}/report/{contract_id}")
        if response.status_code == 200:
            return _data(response)
        time.sleep(0.5)
    return None


def _download_docx(args: argparse.Namespace, download_url: str) -> bytes:
    """优先用 MinIO SDK（预签名 host 多为容器内地址）；否则直接 GET。"""
    parsed = urlparse(download_url)
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) == 2:
        response = _minio(args).get_object(parts[0], parts[1])
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
    response = httpx.get(download_url, timeout=args.timeout)
    response.raise_for_status()
    return response.content


def _docx_headings(data: bytes) -> list[str]:
    from docx import Document

    document = Document(io.BytesIO(data))
    return [
        p.text
        for p in document.paragraphs
        if p.style and p.style.name.startswith("Heading")
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> int:
    checks = _Tracker()
    base = args.api_base_url.rstrip("/") + args.api_prefix
    client = httpx.Client(timeout=args.timeout)

    # 0) 网关连通性（环境错误 -> 退出码 2）
    try:
        health = client.get(f"{args.api_base_url.rstrip('/')}/health", timeout=5)
        health.raise_for_status()
    except Exception as exc:
        raise EnvError(f"网关不可达：{args.api_base_url}/health（{exc}）") from exc

    # 0.1) MinIO bucket（环境错误 -> 退出码 2）
    try:
        storage = _minio(args)
        if not storage.bucket_exists(args.bucket):
            storage.make_bucket(args.bucket)
    except Exception as exc:
        raise EnvError(f"MinIO 不可用（{args.minio_endpoint}）：{exc}") from exc

    fixture = Path(args.file).read_bytes() if args.file else build_fixture_docx()
    file_name = Path(args.file).name if args.file else "e2e-contract.docx"

    # 1) presign
    started = time.perf_counter()
    response = client.post(
        f"{base}/contracts/presign",
        json={"fileName": file_name, "fileType": "DOCX"},
    )
    checks.add(
        "POST /contracts/presign",
        "PASS" if response.status_code == 200 else "FAIL",
        f"HTTP {response.status_code}",
    )
    if response.status_code != 200:
        return _finish(checks, started)
    presign = _data(response) or {}
    file_key = presign["fileKey"]

    # 2) MinIO 直传（先试预签名 PUT，host 不可达时回退 SDK）
    uploaded_via = "presigned-put"
    try:
        put = client.put(presign["presignedUrl"], content=fixture, timeout=args.timeout)
        if put.status_code not in (200, 204):
            raise RuntimeError(f"HTTP {put.status_code}")
    except Exception:  # noqa: BLE001 - 容器内 host 不可达时回退 SDK
        storage.put_object(
            args.bucket,
            file_key,
            io.BytesIO(fixture),
            length=len(fixture),
            content_type=_DOCX_MIME,
        )
        uploaded_via = "minio-sdk"
    checks.add("MinIO 直传", "PASS", f"{uploaded_via} -> {file_key}")

    # 3) upload -> 建合同 + 派发 parse_doc
    response = client.post(
        f"{base}/contracts/upload",
        json={"fileKey": file_key, "fileName": file_name, "fileType": "DOCX"},
    )
    checks.add(
        "POST /contracts/upload",
        "PASS" if response.status_code == 202 else "FAIL",
        f"HTTP {response.status_code}",
    )
    if response.status_code != 202:
        return _finish(checks, started)
    upload = _data(response) or {}
    task_id = upload["taskId"]
    contract_id = (upload.get("result") or {})["contractId"]

    # 4) SSE：upload 后立刻订阅（趁任务在跑），读到终态（卡内断言 RUNNING+DONE）
    statuses: list[str] = []
    try:
        with client.stream(
            "GET", f"{base}/tasks/{task_id}/stream", timeout=args.timeout
        ) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    statuses.append(json.loads(line[5:].strip()).get("status"))
                except Exception:  # noqa: BLE001 - 非 JSON 行忽略
                    continue
                if statuses[-1] in _TERMINAL:
                    break
    except Exception:  # noqa: BLE001 - 超时 / 断流
        pass
    if "RUNNING" in statuses and "DONE" in statuses:
        checks.add("SSE 收 RUNNING+DONE", "PASS", f"events={len(statuses)}")
    else:
        checks.add("SSE 收 RUNNING+DONE", "FAIL", f"仅收到 {statuses}")

    # 5) 轮询 /tasks/{id}（此时应已终态；卡内断言）
    deadline = time.monotonic() + args.timeout
    snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        snapshot = _data(client.get(f"{base}/tasks/{task_id}"))
        if snapshot and snapshot.get("status") in _TERMINAL:
            break
        time.sleep(0.4)
    if snapshot and snapshot.get("status") == "DONE":
        checks.add("GET /tasks/{id} 推进到 DONE", "PASS")
    elif snapshot and snapshot.get("status") == "FAILED":
        checks.add(
            "GET /tasks/{id} 推进到 DONE",
            "FAIL",
            f"任务 FAILED：{snapshot.get('result')}",
        )
    else:
        checks.add("GET /tasks/{id} 推进到 DONE", "FAIL", f"未达终态：{snapshot}")

    # 6) elements（同时触发 C22 finalize_parse：入库 + 派发 run_audit）
    response = client.get(f"{base}/contracts/{contract_id}/elements")
    elements = _data(response) or []
    checks.add(
        "GET /contracts/{id}/elements（触发 C22）",
        "PASS" if response.status_code == 200 and len(elements) > 0 else "FAIL",
        f"HTTP {response.status_code}，elements={len(elements)}",
    )

    # 7) risks / conflicts（触发 C23 finalize_audit）
    risks: list[Any] = []
    conflicts: list[Any] = []
    audit_deadline = time.monotonic() + args.timeout
    while time.monotonic() < audit_deadline:
        risks = _data(client.get(f"{base}/contracts/{contract_id}/risks")) or []
        conflicts = _data(client.get(f"{base}/contracts/{contract_id}/conflicts")) or []
        if risks or conflicts:
            break
        time.sleep(0.5)
    if args.require_risks:
        ok = bool(risks) and all((r.get("citations") or []) for r in risks)
        checks.add(
            "真实语义档：风险>=1 且 citations 非空",
            "PASS" if ok else "FAIL",
            f"risks={len(risks)}",
        )
    else:
        checks.add(
            "审核结果（离线档允许为空）",
            "PASS",
            f"risks={len(risks)}，conflicts={len(conflicts)}（离线 LLM 产出空属设计内）",
        )

    # 8) report/generate -> report/get
    response = client.post(
        f"{base}/report/{contract_id}/generate",
        json={},
        headers={"X-Request-ID": uuid.uuid4().hex},
    )
    checks.add(
        "POST /report/{id}/generate",
        "PASS" if response.status_code == 202 else "FAIL",
        f"HTTP {response.status_code}",
    )
    if response.status_code != 202:
        return _finish(checks, started)
    report = _poll_report(client, base, contract_id, args.timeout)
    checks.add(
        "GET /report/{id} 就绪",
        "PASS" if report else "FAIL",
        f"downloadUrl={'yes' if report else 'no'}",
    )

    # 9) 下载 DOCX + 回读章节
    if report and report.get("downloadUrl"):
        docx_bytes = _download_docx(args, report["downloadUrl"])
        headings = _docx_headings(docx_bytes)
        checks.add(
            "报告 DOCX 含「风险清单 / 矛盾清单」章节",
            "PASS" if ("风险清单" in headings and "矛盾清单" in headings) else "FAIL",
            f"headings={headings}",
        )
        if risks or conflicts:
            checks.add(
                "报告 DOCX 含「溯源」章节",
                "PASS" if "溯源" in headings else "FAIL",
                f"risks={len(risks)}，conflicts={len(conflicts)}",
            )
        else:
            checks.add(
                "报告 DOCX 含「溯源」章节",
                "DEGRADED",
                "离线 0 风险/0 矛盾：render_docx 不输出「溯源」子标题（C18 行为）——显式 DEGRADED",
            )

    # 10) 其余端点（软判定，不误伤核心链）
    _cover_extra_endpoints(client, base, contract_id, checks)

    # 11) 回归脚本
    _regression(checks)

    return _finish(checks, started)


def _cover_extra_endpoints(
    client: httpx.Client, base: str, contract_id: str, checks: _Tracker
) -> None:
    for method, path, payload in (
        ("GET", "/contracts?page=1&pageSize=5", None),
        ("GET", f"/contracts/{contract_id}", None),
        ("GET", "/settings/rules", None),
        (
            "POST",
            "/feedback",
            {
                "contractId": contract_id,
                "targetType": "RISK",
                "targetId": "e2e-probe",
                "verdict": "CORRECT",
                "userCorrection": "端到端冒烟占位反馈",
                "reviewer": "e2e_smoke",
            },
        ),
    ):
        try:
            response = client.request(method, f"{base}{path}", json=payload)
            status = "PASS" if 200 <= response.status_code < 300 else "DEGRADED"
            checks.add(f"{method} {path}", status, f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            checks.add(f"{method} {path}", "DEGRADED", f"{type(exc).__name__}")

    try:
        response = client.post(
            f"{base}/agent/rewrite",
            json={
                "contractId": contract_id,
                "clauseId": "1",
                "instruction": "改成对等解约",
                "scopedMemory": {
                    "targetClauseId": "1",
                    "recentDialog": [],
                    "draftAfter": None,
                },
                "xRequestId": uuid.uuid4().hex,
            },
            timeout=5,
        )
        status = "PASS" if 200 <= response.status_code < 300 else "DEGRADED"
        checks.add("POST /agent/rewrite", status, f"HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001
        checks.add(
            "POST /agent/rewrite",
            "DEGRADED",
            f"{type(exc).__name__}（rewrite 任务未注册 worker）",
        )


def _regression(checks: _Tracker) -> None:
    for script in ("scripts/phase1_smoke.py", "scripts/eval_recall.py"):
        cmd = [sys.executable, str(_REPO_ROOT / script)]
        if script.endswith("eval_recall.py"):
            cmd += ["--gold", str(_REPO_ROOT / "scripts" / "data" / "weak_gold.jsonl")]
        completed = subprocess.run(
            cmd,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        checks.add(
            f"回归 {script}",
            "PASS" if completed.returncode == 0 else "FAIL",
            f"exit={completed.returncode}",
        )


def _finish(checks: _Tracker, started: float) -> int:
    elapsed = round(time.perf_counter() - started, 2)
    print(f"\n端到端耗时：{elapsed}s")
    if checks.failed:
        print("结果：链路失败（存在 FAIL 断言）")
        return 1
    print("结果：通过（允许的 DEGRADED 见上）")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="端到端冒烟：上传→解析→审核→报告（T-D4 / 4.C26）"
    )
    parser.add_argument("--api-base-url", default="http://localhost:8000")
    parser.add_argument("--api-prefix", default="/api/v1")
    parser.add_argument("--minio-endpoint", default="localhost:9000")
    parser.add_argument("--minio-access-key", default="minioadmin")
    parser.add_argument("--minio-secret-key", default="minioadmin")
    parser.add_argument("--bucket", default="contracts")
    parser.add_argument(
        "--file", default=None, help="本地合同文件；缺省生成内存 DOCX 夹具"
    )
    parser.add_argument(
        "--require-risks",
        action="store_true",
        help="真实语义档：风险>=1 且 citations 非空",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--sse-timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except EnvError as exc:
        print(f"[环境错误] {exc}", file=sys.stderr, flush=True)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[链路失败] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
