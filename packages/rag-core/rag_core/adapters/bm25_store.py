"""BM25 稀疏检索：实现 `rag_contracts.ports.SparseStorePort`。

零新增依赖：复用 `rag_core.adapters.embedding.tokenize` 切词（拉丁词 + CJK 单字 + CJK 二元组，无需 jieba）。
磁盘持久化：pickle 单文件；同一 chunk_id 先删后写，保证幂等重跑。

对应：RAG搭建框架.md §6.2 / §8.5，RAG白皮书.md §3.2.2。
"""

from __future__ import annotations

import math
import pickle
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from rag_contracts.chunk import ContractChunk
from rag_contracts.ports import SparseHit

from .embedding import tokenize

# BM25 超参（沿用前作 RAG_IMPLEMENTATION_GUIDE，见 §8.5）
_K1 = 1.5  # 词频饱和参数
_B = 0.75  # 长度归一化强度


# 注 1：非线程安全，适合单线程或外部加锁使用（§6.2 并发安全：❌ 非线程安全）。
# 注 2：【偏离规范】§6.2 / §8.5 将本类描述为“倒排索引”，但内部 `_tf` 实为
#       chunk_id → {term: freq} 的**正排表**、`search` 为全量扫描（无 term → postings），
#       故 docstring 改为“正排表扫描”以免误导后续维护者。请勿按规范逐字改回。
class BM25Store:
    """基于内存正排表扫描的 BM25 检索器（disk-persisted, idempotent upsert）。

    内部状态：
        _tf         : chunk_id -> {term: freq}
        _texts      : chunk_id -> 原文（供下游重排取文本，见 SparseHit.text）
        _doc_len    : chunk_id -> token 数
        _doc_source : chunk_id -> doc_id（支撑 delete_by_doc）
        _df         : term -> 多少篇文档含它
        _avgdl      : 全库平均文档长度
        _payloads   : chunk_id -> 完整 ContractChunk payload（供稀疏路独占候选还原，T-A0-2）
    """

    def __init__(self, data_dir: str | Path = "./data/bm25") -> None:
        """初始化并加载已存在的索引。

        Args:
            data_dir: 索引目录；不存在则创建。
        """
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tf: dict[str, dict[str, int]] = {}
        self._texts: dict[str, str] = {}
        self._doc_len: dict[str, int] = {}
        self._doc_source: dict[str, str] = {}
        self._df: defaultdict[str, int] = defaultdict(int)
        self._avgdl = 0.0
        self._payloads: dict[str, dict] = {}
        self._load()

    # ---------------- 持久化 ----------------
    @property
    def _path(self) -> Path:
        """索引文件路径（固定 index.pkl，见 §2.5）。"""
        return self._dir / "index.pkl"

    def _load(self) -> None:
        """启动时加载索引。文件不存在或损坏时静默降级为空索引。

        元组顺序必须与 save() 严格一致 —— 顺序错位会导致 TF/DF 静默错乱。
        T-A0-2 起为 7 元组（新增 `_payloads`）：**必须显式兼容旧 6 元组**，
        否则老索引会被 try/except 当成「损坏」而整库静默重建（索引白丢）。
        """
        if not self._path.exists():
            return
        try:
            blob = pickle.loads(self._path.read_bytes())
            if len(blob) == 7:
                (
                    self._tf,
                    self._texts,
                    self._doc_len,
                    self._doc_source,
                    self._df,
                    self._avgdl,
                    self._payloads,
                ) = blob
            else:
                # 兼容旧版 6 元组：payload 缺失，置空（下次 save() 自动升级为 7 元组）
                (
                    self._tf,
                    self._texts,
                    self._doc_len,
                    self._doc_source,
                    self._df,
                    self._avgdl,
                ) = blob
                self._payloads = {}
        except Exception:  # noqa: BLE001 - 索引损坏时重建优于崩溃
            self._tf, self._texts = {}, {}
            self._doc_len, self._doc_source = {}, {}
            self._df, self._avgdl = defaultdict(int), 0.0
            self._payloads = {}
        else:
            self._df = defaultdict(int, self._df)

    def save(self) -> None:
        """落盘（全量覆盖写）。"""
        self._path.write_bytes(
            pickle.dumps(
                (
                    self._tf,
                    self._texts,
                    self._doc_len,
                    self._doc_source,
                    dict(self._df),
                    self._avgdl,
                    self._payloads,
                )
            )
        )

    # ---------------- SparseStorePort ----------------
    def upsert(self, chunks: Sequence[ContractChunk]) -> int:
        """幂等写入：同一 chunk_id 先删旧索引再写新索引。

        Args:
            chunks: 切片序列。

        Returns:
            写入条数。
        """
        for chunk in chunks:
            self._remove(chunk.chunk_id)
            tf: defaultdict[str, int] = defaultdict(int)
            for term in tokenize(chunk.text):
                tf[term] += 1
            self._tf[chunk.chunk_id] = dict(tf)
            self._texts[chunk.chunk_id] = chunk.text
            self._doc_len[chunk.chunk_id] = sum(tf.values())
            self._doc_source[chunk.chunk_id] = chunk.doc_id
            # T-A0-2：payload 一并入库，使稀疏路独占候选可还原完整切片
            self._payloads[chunk.chunk_id] = chunk.to_payload()
            # DF 只关心"是否出现"，故遍历去重后的词集合
            for term in tf:
                self._df[term] += 1
        self._refresh_avgdl()
        self.save()
        # 【偏离规范】§6.2 样例为 `len(list(chunks))`，此处故意不逐字对齐：
        # 参数已声明 Sequence，`list()` 多一次 O(n) 复制；若调用方传入生成器，
        # 生成器已被上方 `for` 耗尽，`len(list(chunks))` 会错误返回 0。
        return len(chunks)

    def search(self, query: str, top_k: int = 20) -> list[SparseHit]:
        """BM25 打分并取 Top-K。

        实现要点（见 §8.5）：
        - idf <= 0 的词直接跳过（该词过于普遍，无区分度）；
        - text 一并回传，否则下游重排拿不到文本。

        Args:
            query: 查询文本。
            top_k: 返回条数上限。

        Returns:
            按 BM25 分降序的命中列表；空库返回 []。
        """
        if not self._tf or not query:
            return []
        n = len(self._doc_len)
        avgdl = self._avgdl or 1.0
        scores: dict[str, float] = {}
        for term in set(tokenize(query)):
            df = self._df.get(term, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            if idf <= 0:
                continue
            for chunk_id, tf in self._tf.items():
                freq = tf.get(term, 0)
                if freq == 0:
                    continue
                doc_len = self._doc_len[chunk_id]
                denom = freq + _K1 * (1 - _B + _B * doc_len / avgdl)
                scores[chunk_id] = (
                    scores.get(chunk_id, 0.0) + idf * freq * (_K1 + 1) / denom
                )
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[
            : max(0, top_k)
        ]
        return [
            SparseHit(
                chunk_id=cid,
                score=float(score),
                text=self._texts.get(cid, ""),
                payload=self._payloads.get(cid, {}),
            )
            for cid, score in ranked
        ]

    def delete_by_doc(self, doc_id: str) -> int:
        """按 doc_id 删除全部切片并回退 DF。

        Args:
            doc_id: 源文档标识。

        Returns:
            删除条数。
        """
        targets = [cid for cid, src in self._doc_source.items() if src == doc_id]
        for chunk_id in targets:
            self._remove(chunk_id)
        self._refresh_avgdl()
        self.save()
        return len(targets)

    def health(self) -> bool:
        """健康检查：本实现为纯本地结构，恒可用。"""
        return True

    # ---------------- 内部 ----------------
    def _remove(self, chunk_id: str) -> None:
        """移除单个 chunk 的索引并**回退 DF**（否则 IDF 会失真）。

        Args:
            chunk_id: 切片 ID。
        """
        if chunk_id not in self._tf:
            return
        for term in self._tf[chunk_id]:
            self._df[term] = max(0, self._df[term] - 1)
        self._tf.pop(chunk_id, None)
        self._texts.pop(chunk_id, None)
        self._doc_len.pop(chunk_id, None)
        self._doc_source.pop(chunk_id, None)
        self._payloads.pop(chunk_id, None)

    def _refresh_avgdl(self) -> None:
        """重算平均文档长度（每次增删后必须调用）。"""
        total = sum(self._doc_len.values())
        self._avgdl = (total / len(self._doc_len)) if self._doc_len else 0.0

    # ---------------- 调试辅助 ----------------
    def stats(self) -> dict[str, float | int]:
        """返回索引规模统计（供埋点 upsert.done 使用）。"""
        return {
            "docs": len(self._tf),
            "terms": len(self._df),
            "avgdl": round(self._avgdl, 4),
        }


__all__ = ["BM25Store"]
