"""报告渲染任务（§10 步骤10：Puppeteer / WeasyPrint 报告渲染）。

流程：汇聚 Contract + 风险 + 矛盾 + 印章 + 健康分 → 渲染 PDF/HTML →
回传 MinIO 生成下载 URL，更新 Contract.reportId。
"""

from __future__ import annotations

from worker.main import celery_app

from worker.storage import minio_client


@celery_app.task(name="gen_report", bind=True)
def gen_report_task(self, contract_id: str) -> dict:
    """渲染并导出审核报告。

    TODO（工程实现）：
      1. 聚合审核结果与健康分；
      2. 经 WeasyPrint / Puppeteer 渲染（雷达图 + 风险卡片 + Diff）；
      3. 上传 MinIO 并返回 downloadUrl，更新 Contract.reportId。
    """
    client = minio_client.get_client()
    raise NotImplementedError("gen_report_task 待实现：渲染 + MinIO 回传")
