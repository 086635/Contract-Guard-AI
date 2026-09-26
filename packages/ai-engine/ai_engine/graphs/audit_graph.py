"""审核图固定拓扑。

设计要点：
1. 图只有六个固定节点，条款循环必须在节点内部完成。
2. 依赖通过 functools.partial 闭包绑定，不进入 state。
3. checkpointer 由参数注入，不在本模块选择开发或生产实现。
"""

from __future__ import annotations

from functools import partial
from itertools import pairwise

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ai_engine.graphs.deps import GraphDeps
from ai_engine.graphs.nodes import (
    classify_node,
    conflict_node,
    extract_node,
    report_draft_node,
    risk_node,
    standard_compare_node,
)
from ai_engine.graphs.state import AUDIT_STAGES, AuditState

_NODES = (
    classify_node,
    extract_node,
    risk_node,
    conflict_node,
    standard_compare_node,
    report_draft_node,
)


def build_audit_graph(
    deps: GraphDeps,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """构造并编译固定六阶段同步审核图。"""
    graph = StateGraph(AuditState)
    for stage, node in zip(AUDIT_STAGES, _NODES, strict=True):
        graph.add_node(stage, partial(node, deps=deps))

    graph.add_edge(START, AUDIT_STAGES[0])
    for current_stage, next_stage in pairwise(AUDIT_STAGES):
        graph.add_edge(current_stage, next_stage)
    graph.add_edge(AUDIT_STAGES[-1], END)
    return graph.compile(checkpointer=checkpointer)


__all__ = ["build_audit_graph"]
