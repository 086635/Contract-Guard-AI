"""Agent 图状态与骨架公开入口。

状态、依赖容器和固定拓扑在本包导出；业务节点分别由 C5~C8 实现。
"""

from __future__ import annotations

from .audit_graph import build_audit_graph
from .deps import GraphDeps
from .rewrite_graph import REWRITE_STAGES, build_rewrite_graph
from .state import (
    AUDIT_STAGES,
    AuditState,
    ClauseInput,
    RewriteState,
    StageLog,
    new_audit_state,
)

__all__ = [
    "AUDIT_STAGES",
    "REWRITE_STAGES",
    "AuditState",
    "ClauseInput",
    "GraphDeps",
    "RewriteState",
    "StageLog",
    "build_audit_graph",
    "build_rewrite_graph",
    "new_audit_state",
]
