"""AI Agent 逻辑包（合同智能审核引擎）。

聚合三大智能体（auditor / rewriter / conflict）、分层 Memory 管理器、
RAG 检索与重排引擎，以及 Prompt 模版库。

所有对外输出必须收敛到 data_scheme.md §9 领域模型，并携带 §8.4 的
ReasoningStep / PreciseCitation，禁止返回未声明字段。
"""

__version__ = "1.1.0"
