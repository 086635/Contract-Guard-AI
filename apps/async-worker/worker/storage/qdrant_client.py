"""Qdrant 向量库客户端（RAG 检索与溯源）。"""

from __future__ import annotations

import os

from qdrant_client import QdrantClient


def get_qdrant_client() -> QdrantClient:
    """按环境变量构建 Qdrant 客户端。"""
    return QdrantClient(url=os.getenv("CG_QDRANT_URL", "http://localhost:6333"))
