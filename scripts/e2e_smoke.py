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



def _check_cors_preflight(
    client: httpx.Client,
    url: str,
    origin: str,
    method: str,
    checks: _Tracker,
    label: str,
) -> bool:
    """检查浏览器预检与允许来源头。"""
    try:
        response = client.request(
            "OPTIONS",
            url,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
            },
        )
        allow_origin = response.headers.get("access-control-allow-origin")
        ok = response.status_code in (200, 204) and allow_origin == origin
        checks.add(
            f"CORS 预检 {label}",
            "PASS" if ok else "FAIL",
            f"HTTP {response.status_code}，ACAO={allow_origin}",
        )
        return ok
    except Exception as exc:  # noqa: BLE001
        checks.add(f"CORS 预检 {label}", "FAIL", f"{type(exc).__name__}: {exc}")
        return False


def _check_elements_extra(elements: list[Any], checks: _Tracker) -> None:
    """确认 ContractElement value/confidence/isCustom 契约形状。"""
    if not elements:
        checks.add("elements extra 投影", "FAIL", "elements 为空")
        return
    bad: list[str] = []
    non_null = 0
    for index, item in enumerate(elements):
        value = item.get("value")
        confidence = item.get("confidence")
        is_custom = item.get("isCustom")
        if value is not None:
            non_null += 1
            if not isinstance(value, str):
                bad.append(f"#{index}.value")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            bad.append(f"#{index}.confidence")
        elif confidence < 0 or confidence > 1:
            bad.append(f"#{index}.confidence-range")
        if not isinstance(is_custom, bool):
            bad.append(f"#{index}.isCustom")
    checks.add(
        "elements extra 投影",
        "PASS" if not bad else "FAIL",
        f"elements={len(elements)}，nonNullValue={non_null}，bad={bad[:3]}",
    )


def _check_feedback_roundtrip(
    client: httpx.Client,
    base: str,
    contract_id: str,
    checks: _Tracker,
) -> None:
    """POST 反馈后按合同 GET 重读，证明持久化与列表契约。"""
    target_id = f"e2e-feedback-{uuid.uuid4().hex[:8]}"
    try:
        created = client.post(
            f"{base}/feedback",
            json={
                "contractId": contract_id,
                "targetType": "RISK",
                "targetId": target_id,
                "verdict": "CORRECT",
                "userCorrection": "端到端 C43 反馈持久化探针",
                "reviewer": "e2e_smoke",
            },
        )
        created_data = _data(created) or {}
        listed = client.get(
            f"{base}/feedback",
            params={"contractId": contract_id, "pageSize": 100},
        )
        listed_data = _data(listed) or {}
        items = listed_data.get("items") or []
        found = any(
            str(item.get("id")) == str(created_data.get("id"))
            and str(item.get("contractId")) == str(contract_id)
            and item.get("reviewer") == "e2e_smoke"
            for item in items
        )
        ok = (
            created.status_code == 201
            and listed.status_code == 200
            and found
            and int(listed_data.get("total") or 0) >= 1
        )
        checks.add(
            "反馈 POST→GET 持久化",
            "PASS" if ok else "FAIL",
            f"POST={created.status_code}，GET={listed.status_code}，"
            f"target={target_id}，found={found}",
        )
    except Exception as exc:  # noqa: BLE001
        checks.add("反馈 POST→GET 持久化", "FAIL", f"{type(exc).__name__}: {exc}")


def _check_settings_roundtrip(
    client: httpx.Client,
    base: str,
    checks: _Tracker,
) -> None:
    """GET 规则后 PUT 测试值，再 GET 重读并恢复原值。"""
    try:
        listed = client.get(f"{base}/settings/rules")
        rules = _data(listed) or []
        if listed.status_code != 200:
            checks.add("设置 GET→PUT→GET 持久化", "FAIL", f"GET HTTP {listed.status_code}")
            return
        if not rules:
            checks.add(
                "设置 GET→PUT→GET 持久化",
                "DEGRADED",
                "当前 e2e 库无 settings_rules，需运行前预置一条规则",
            )
            return

        rule = rules[0]
        rule_id = rule["id"]
        original_enabled = rule.get("enabled")
        original_thresholds = rule.get("reviewThresholds")
        test_thresholds = {"high": 0.80, "medium": 0.60, "low": 0.30}
        updated = {
            **rule,
            "enabled": not bool(original_enabled),
            "reviewThresholds": test_thresholds,
        }
        put = client.put(f"{base}/settings/rules/{rule_id}", json=updated)
        reread = client.get(f"{base}/settings/rules/{rule_id}")
        reread_data = _data(reread) or {}
        roundtrip_ok = (
            put.status_code == 200
            and reread.status_code == 200
            and reread_data.get("enabled") is updated["enabled"]
            and reread_data.get("reviewThresholds") == test_thresholds
        )
        restore = client.put(
            f"{base}/settings/rules/{rule_id}",
            json={
                **updated,
                "enabled": original_enabled,
                "reviewThresholds": original_thresholds,
            },
        )
        restore_ok = restore.status_code == 200
        checks.add(
            "设置 GET→PUT→GET 持久化",
            "PASS" if roundtrip_ok and restore_ok else "FAIL",
            f"PUT={put.status_code}，GET={reread.status_code}，"
            f"roundtrip={roundtrip_ok}，restore={restore_ok}",
        )
    except Exception as exc:  # noqa: BLE001
        checks.add("设置 GET→PUT→GET 持久化", "FAIL", f"{type(exc).__name__}: {exc}")


def _check_rewrite(
    client: httpx.Client,
    base: str,
    contract_id: str,
    timeout: float,
    *,
    allow_offline: bool,
    checks: _Tracker,
) -> None:
    """调用真实 rewrite Worker 并检查 RewriteData 或显式失败。"""
    request_id = uuid.uuid4().hex
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
                "xRequestId": request_id,
            },
            timeout=timeout,
        )
        data = _data(response) or {}
        ok = (
            response.status_code == 200
            and data.get("taskId") == request_id
            and isinstance(data.get("diff"), dict)
            and isinstance(data.get("after"), str)
        )
        detail = str(response.text or "")
        if ok:
            status = "PASS"
        elif allow_offline and response.status_code == 502:
            status = "DEGRADED"
            detail = "Worker 已注册并返回显式 502；当前 offline 模型无真实改写输出"
        else:
            status = "FAIL"
        checks.add(
            "POST /agent/rewrite Worker 链",
            status,
            f"HTTP {response.status_code}，taskId={data.get('taskId')}，{detail}",
        )
    except Exception as exc:  # noqa: BLE001
        checks.add(
            "POST /agent/rewrite 真实结果",
            "FAIL",
            f"{type(exc).__name__}: {exc}",
        )


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
    _check_cors_preflight(
        client,
        presign["presignedUrl"],
        "http://localhost:5173",
        "PUT",
        checks,
        "上传 PUT",
    )

    # 2) MinIO 浏览器直传：必须走可访问的 presign URL；SDK 仅作诊断回退。
    uploaded_via = "presigned-put"
    direct_error: Exception | None = None
    try:
        put = client.put(
            presign["presignedUrl"],
            content=fixture,
            headers={"Origin": "http://localhost:5173"},
            timeout=args.timeout,
        )
        allow_origin = put.headers.get("access-control-allow-origin")
        if put.status_code not in (200, 204) or allow_origin != "http://localhost:5173":
            raise RuntimeError(f"HTTP {put.status_code}，ACAO={allow_origin}")
    except Exception as exc:  # noqa: BLE001
        direct_error = exc
        storage.put_object(
            args.bucket,
            file_key,
            io.BytesIO(fixture),
            length=len(fixture),
            content_type=_DOCX_MIME,
        )
        uploaded_via = "minio-sdk"
    direct_detail = f"{uploaded_via} -> {file_key}"
    if direct_error is not None:
        direct_detail += f"；{type(direct_error).__name__}: {direct_error}"
    checks.add(
        "MinIO 浏览器直传",
        "PASS" if direct_error is None else "FAIL",
        direct_detail,
    )

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
    _check_elements_extra(elements, checks)

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

    # 9) 浏览器 presign GET + 下载 DOCX + 回读章节
    if report and report.get("downloadUrl"):
        download_url = report["downloadUrl"]
        _check_cors_preflight(
            client,
            download_url,
            "http://localhost:8080",
            "GET",
            checks,
            "报告 GET",
        )
        try:
            download = client.get(
                download_url,
                headers={"Origin": "http://localhost:8080"},
                timeout=args.timeout,
            )
            download.raise_for_status()
            allow_origin = download.headers.get("access-control-allow-origin")
            if allow_origin != "http://localhost:8080":
                raise RuntimeError(f"ACAO={allow_origin}")
            docx_bytes = download.content
            checks.add(
                "报告 presign 浏览器 GET",
                "PASS",
                f"HTTP {download.status_code}，bytes={len(docx_bytes)}",
            )
        except Exception as exc:  # noqa: BLE001
            checks.add(
                "报告 presign 浏览器 GET",
                "FAIL",
                f"{type(exc).__name__}: {exc}",
            )
            docx_bytes = _download_docx(args, download_url)
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
    _cover_extra_endpoints(
        client,
        base,
        contract_id,
        args.timeout,
        allow_offline_rewrite=args.allow_offline_rewrite,
        checks=checks,
    )

    # 11) 回归脚本
    _regression(checks)

    return _finish(checks, started)


def _cover_extra_endpoints(
    client: httpx.Client,
    base: str,
    contract_id: str,
    timeout: float,
    *,
    allow_offline_rewrite: bool,
    checks: _Tracker,
) -> None:
    """覆盖 T-D6 新增契约，不再把关键链路降级为软通过。"""
    for method, path in (
        ("GET", "/contracts?page=1&pageSize=5"),
        ("GET", f"/contracts/{contract_id}"),
    ):
        try:
            response = client.request(method, f"{base}{path}")
            status = "PASS" if 200 <= response.status_code < 300 else "FAIL"
            checks.add(f"{method} {path}", status, f"HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001
            checks.add(f"{method} {path}", "FAIL", f"{type(exc).__name__}")

    _check_feedback_roundtrip(client, base, contract_id, checks)
    _check_settings_roundtrip(client, base, checks)
    _check_rewrite(
        client,
        base,
        contract_id,
        timeout,
        allow_offline=allow_offline_rewrite,
        checks=checks,
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
    parser.add_argument(
        "--allow-offline-rewrite",
        action="store_true",
        help="离线档允许 rewrite 显式 502 记为 DEGRADED；不得伪造 RewriteData",
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
