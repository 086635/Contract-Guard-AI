"""模块 3（doc-processor）的错误体系。

设计要点：
1. 基类 `DocProcessorError` **刻意不继承 `rag_contracts.errors.RagError`** ——
   `doc_processor` 原本不依赖契约层（解析层属被消费方，反向依赖会破坏分层）。
   异常语义与 `RagError` 保持一致（可读消息 + 可判定类型），但**不共享基类**。
2. 子类只允许在**对应任务**落盘时新增（如 3-3 的 `OcrEngineUnavailableError` /
   `RenderFailedError`），不得提前声明"以后可能用得到"的异常。
"""

from __future__ import annotations

from pathlib import Path


class DocProcessorError(Exception):
    """模块 3 全部异常的基类。"""


class UnsupportedFormatError(DocProcessorError):
    """输入文件不可用：扩展名不在支持集合内，或文件不存在 / 不可读 / 已损坏。

    🔴 **不得**把"读取失败"静默降级为 `PDF_SCANNED` —— 那会让一个坏文件白跑几分钟 OCR
    （`3-2 §⑨-4`）。

    Attributes:
        path: 触发异常的文件路径（字符串形式，便于日志序列化）。
        reason: 一句中文原因（进日志与 `RouteDecision` 的排查链路）。
    """

    def __init__(self, path: str | Path, reason: str) -> None:
        """初始化。

        Args:
            path: 输入文件路径。
            reason: 中文原因说明。
        """
        self.path = str(path)
        self.reason = reason
        super().__init__(f"不可用的输入文件：{self.path}（{reason}）")


__all__ = ["DocProcessorError", "UnsupportedFormatError"]
