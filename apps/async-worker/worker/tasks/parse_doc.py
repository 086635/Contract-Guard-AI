"""文档解析与 OCR 任务（§10 步骤2：解析）。

流程：从 MinIO 拉取源文件 → doc_processor.layout/ocr/parser/aligner 抽取
结构化要素、条款 AST、印章 → 落库并更新 Contract.status。
"""

from __future__ import annotations

from worker.main import celery_app

from doc_processor.layout import LayoutAnalyzer
from doc_processor.ocr import OcrEngine
from doc_processor.parser import NodeExtractor


@celery_app.task(name="parse_doc", bind=True)
def parse_doc_task(self, file_key: str, contract_id: str) -> dict:
    """解析单个合同文档。

    TODO（工程实现）：
      1. 经 storage 客户端从 MinIO 拉取 file_key；
      2. 调用 LayoutAnalyzer / OcrEngine / NodeExtractor / BBoxAligner；
      3. 写回 DB 与 Qdrant，更新 Contract.status = PENDING_REVIEW。
    """
    analyzer = LayoutAnalyzer()
    ocr = OcrEngine()
    extractor = NodeExtractor()
    # 骨架占位：真实实现需下载文件并驱动上述引擎
    raise NotImplementedError("parse_doc_task 待实现：接入 doc_processor + storage")
