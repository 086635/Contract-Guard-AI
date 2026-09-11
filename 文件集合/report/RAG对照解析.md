# RAG 对照解析 —— 每个小组件的代码级实现拆解

> **本文是《RAG白皮书.md》的配套解析**。白皮书告诉你"有什么、契约是什么"；本文告诉你"**每一块到底怎么用代码算出来的**"。
> **阅读对象**：已经知道"向量化 / 向量库 / top-k / 精排"这些名词，但不知道它们**具体怎么用代码实现**的开发者。
> **方法**：每个组件都按 **① 它解决什么问题 → ② 最小可运行实现（逐行逻辑注释） → ③ 手算一遍真实数字 → ④ 对照项目里的真实类** 四步展开。
> **所有数字都是实测跑出来的**，不是编的（含一个"做错会怎样"的反例演示）。

---

## 0. 阅读指南与贯穿全文的玩具语料

全文复用同一份微型语料，方便你对照每一步的变形：

```python
CORPUS = {
    "c0": "买方逾期付款的，应按日支付合同总价千分之五的违约金",
    "c1": "违约金总额累计不超过合同总价的百分之十",
    "c2": "试用期最长不得超过六个月",
}
QUERY = "逾期付款违约金上限"
```

**期望结果**：`c0`（讲逾期付款违约金）最相关，`c1`（讲违约金上限）次之，`c2`（试用期）完全不相关。

**三段式说明**

| 段落 | 用途 | 能否直接跑 |
|---|---|---|
| 「最小实现」代码块 | 去掉所有工程封装，只留算法骨架，逐行注释 | ✅ 纯 Python，可直接 `python` 运行 |
| 「手算」表格 | 展示真实中间数值，理解数据怎么变形 | — |
| 「对照真实类」 | 指回项目里真正的类名与文件位置 | — |

---

## 第 0 章 先建立全局直觉：一次检索到底发生了什么

把 `QUERY` 喂进去，真实数字是这样流动的（实测值）：

| 步骤 | 输入 | 做了什么 | 输出（真实数字） |
|---|---|---|---|
| ① 切词 | `"逾期付款违约金上限"` | 切成中文二元组 | `['逾期','期付','付款','款违','违约','约金','金上','上限']` |
| ② 向量化 | 词列表 | hashing trick → 1024 维向量 | `[0.0, 0.0, ..., 0.25, ...]`（长度 1024） |
| ③ 存 | 3 篇文档 | 每篇算一个向量，放进 `dict` | `{"c0": vec0, "c1": vec1, "c2": vec2}` |
| ④ 稠密召回 | query 向量 | 与每个文档向量算余弦 | `c0=0.3467, c1=0.2372, c2=0.0` |
| ⑤ 稀疏召回 | query 词 | BM25 打分 | `c0=3.3432, c1=0.9321, c2=0.0` |
| ⑥ 融合 | 两路名次 | RRF (k=60) | `c0=0.032787, c1=0.032258, c2=0.031746` |
| ⑦ 阈值 | 融合结果 | 丢掉低于 0.05 的 | 保留 `c0, c1` |
| ⑧ 组装 | 保留的文档 | 按 token 预算贪心填充 | `chunks=2, tokens=30/30` |

**一句话概括**：RAG 检索 = **把文字变成一串数字 → 比谁的数字离得近 → 再比谁的词对得上 → 把两种"谁更近"的意见合并 → 砍掉不够近的 → 塞进有限的预算里**。

---

## 第 1 章 文本 → 向量（Embedding）

### 1.1 向量到底是什么？先手算一个 8 维的

向量就是**一串有序的数字**，用来表示一段文字的"位置"。两段意思相近的文字，数字串应该"离得近"。

下面把 `"逾期付款违约金上限"` 变成 8 维向量（真实输出）：

```
query vec (8维): [0.354, 0.0, 0.0, 0.707, -0.354, 0.0, -0.354, 0.354]
doc  c0   (8维): [0.0, 0.5, -0.25, -0.5, -0.5, -0.25, -0.25, -0.25]   余弦 = -0.1768
doc  c1   (8维): [0.365, -0.183, -0.73, 0.0, 0.0, -0.365, 0.183, 0.365] 余弦 =  0.1936
doc  c2   (8维): [0.447, 0.0, 0.447, 0.447, -0.447, 0.447, 0.0, 0.0]   余弦 =  0.6325
```

🚨 **注意这里出错了**：`c2`（试用期）拿到了最高分 0.6325，**完全违背语义**。为什么？见 §1.7。

### 1.2 切词：为什么中文用"二元组"

英文有空格天然分词，中文没有。分词有两种做法：

| 做法 | 例子 | 优劣 |
|---|---|---|
| 单字切 | `违 / 约 / 金` | 噪声大——"违"和"约"单独都出现在很多无关条款里 |
| **二元组（本项目采用）** | `违约 / 约金 / 金上` | **保留语序**：`违约`+`约金` 组合起来才指向"违约金" |
| Jieba 分词 | `违约金 / 上限` | 语义最好，但要装额外依赖，且法律术语词表不全 |

```python
def tokenize(text: str) -> list[str]:
    """中文二元组切词：相邻两字一组，滑动窗口步长 1。
    '违约金上限' -> ['违约', '约金', '金上', '上限']
    这样 '违约' 与 '约金' 两个词同时命中时，才算真的出现了 '违约金'。"""
    return [text[i:i + 2] for i in range(len(text) - 1)]
```

### 1.3 Hashing Trick：不用训练模型，怎么把词变成向量？

真实 embedding 模型（bge-m3）是神经网络算出来的。但**向量化的本质思想**用 10 行就能讲清——这就是项目里 `HashEmbedder` 干的事：

```python
import hashlib
import math

DIM = 1024  # 项目锁定 1024 维（bge-m3 的输出维度）


def embed(text: str, dim: int = DIM) -> list[float]:
    """Hashing Trick 向量化：把每个词'扔'进向量的某个位置。

    逻辑四步：
      1. 准备一个长度 dim 的全 0 向量
      2. 对文本切词
      3. 每个词用哈希算出一个下标 idx，在该位置 +1 或 -1（符号由哈希决定）
      4. 整体做 L2 归一化（见 1.5）
    """
    # 步骤1：全 0 向量，1024 个坑位等着被"点亮"
    vec = [0.0] * dim

    # 步骤2：切词
    for token in tokenize(text):
        # 步骤3a：把词哈希成 8 字节。用 blake2b 而非内置 hash()，原因见 1.4
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()

        # 步骤3b：哈希字节转整数，对 dim 取模 → 得到 0~1023 之间的下标
        #         这就是"这个词应该点亮第几号坑位"
        idx = int.from_bytes(digest, "big") % dim

        # 步骤3c：用哈希的第一个 bit 决定加还是减
        #         目的：让不同的词互相抵消一部分，降低碰撞的影响
        sign = 1.0 if (digest[0] & 1) else -1.0
        vec[idx] += sign

    # 步骤4：L2 归一化
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec
```

**关键理解**：向量里**每个位置本身没有语义**。是"两个文本如果词相同，就会点亮相同的坑位，于是向量相似"这个机制在起作用。

### 1.4 ⚠️ 为什么必须用 `blake2b` 而不是内置 `hash()`

Python 的 `hash()` 对字符串**带随机盐**（`PYTHONHASHSEED`），**每次启动进程结果都不同**。如果你用它做向量化：

- 今天建库时 `"违约金"` 落在第 137 位
- 明天检索时 `"违约金"` 落在第 881 位
- → **永远搜不到**，而且是偶发的，极难排查

自己验证（两条命令输出会不同）：

```bash
PYTHONHASHSEED=1 python -c "print(hash('违约金'))"
PYTHONHASHSEED=2 python -c "print(hash('违约金'))"
```

`blake2b` 是**确定性**的，任何进程、任何机器、任何时间，同一个词永远得到同一个哈希。

### 1.5 L2 归一化是什么，为什么必须要

归一化就是把向量的"长度"缩放到 1，只保留"方向"。

```python
def l2_normalize(vec: list[float]) -> list[float]:
    """把向量缩放成单位长度：每个分量 / 向量总长度。

    为什么要做：
      长文档点亮的位置多，向量天然更长。不做归一化的话，
      '越长'会被误判为'越相关'，长条款会永远排在前面。
      归一化后，比较的纯粹是'方向'（即内容构成比例），与长度无关。
    """
    norm = math.sqrt(sum(v * v for v in vec))   # 欧几里得长度 = √(Σv²)
    if norm == 0:
        return vec                              # 全 0 向量（空文本）保持原样，避免除零
    return [v / norm for v in vec]
```

**手算一遍**：向量 `[3.0, 4.0]` → 长度 `√(9+16)=5` → 归一化后 `[0.6, 0.8]`，长度变成 `√(0.36+0.64)=1`。

> **重要推论**：归一化之后，**余弦相似度就等于点积**（因为分母两个模长都是 1）。项目里 `InMemoryVectorStore` 直接用点积，就是因为这个前提。

### 1.6 批处理 32 条是怎么回事

真实 embedding 服务（TEI / OpenAI）单次 HTTP 请求有开销，所以攒够一批再发：

```python
def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """批量向量化：每 32 条打包一次，减少网络往返。

    注意：返回顺序必须与输入顺序严格一致（下游靠 zip 对齐）。
    """
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]   # 取一批（最后一批可能不足 32）
        out.extend(_post_to_embedding_service(batch))  # 一次 HTTP 请求拿回 32 个向量
    return out
```

### 1.7 🔥 维度实验：8 / 64 / 1024 的真实差异（解释白皮书里那条空召回）

同一个 query，只改维度，排名完全变了（实测）：

| 维度 | 排名结果 | 结论 |
|---|---|---|
| **8** | `c2=0.6325` > `c1=0.1936` > `c0=-0.1768` | ❌ **完全错**。"试用期"排第一 |
| **64** | `c0=0.2004` > `c1=0.1581` > `c2=0.0` | ✅ 对了，但分差很小（0.20 vs 0.16） |
| **1024** | `c0=0.3467` > `c1=0.2372` > `c2=0.0` | ✅ 对了，且分差明显 |

**为什么 8 维会错？** 1024 个坑位缩到 8 个，**大量不同的词被哈希到同一个位置**（哈希碰撞）。`"试用"` 和 `"逾期"` 撞在同一个坑位上，于是"试用期"被误认为与"逾期付款"相关。

> **这直接解释了两件事**：
> 1. 为什么项目锁定 `CG_RAG_VECTOR_DIM=1024`（`ContractChunk` 里还有 `embedding_dim != 1024` 就报错的强校验）。
> 2. 为什么 `phase1_smoke.py` 3 条查询里有 1 条 `EMPTY_RECALL`——`HashEmbedder` **没有语义理解能力**，只是词面哈希，遇到措辞差异大的查询就会全部低于 0.35 阈值。这是**设计预期**，不是 bug。真实语义能力要靠 `CG_RAG_EMBED_MODE=http` 接 bge-m3。

### 1.8 对照真实类

| 本文的玩具函数 | 项目真实类 | 位置 |
|---|---|---|
| `tokenize` | `tokenize`（含拉丁词 + CJK 二元组） | `rag_core/adapters/embedding.py` |
| `embed`（hashing） | `HashEmbedder.embed_one/embed_batch` | 同上 |
| `embed_batch`（HTTP） | `HttpEmbedder`（批大小 32、超时 3000ms、校验维度） | 同上 |
| `l2_normalize` | 内置在 `HashEmbedder.embed_one` 内 | — |

---

## 第 2 章 向量怎么存、怎么查（Vector Store）

### 2.1 最朴素的向量库：一个 dict + 暴力遍历

向量库**没有魔法**，最小实现就是一个字典加一个 for 循环：

```python
class ToyVectorStore:
    """最小向量库：dict 存储 + 暴力遍历检索。"""

    def __init__(self) -> None:
        # chunk_id -> (向量, 元数据)。真实项目里元数据叫 payload
        self._data: dict[str, tuple[list[float], dict]] = {}

    def upsert(self, chunk_id: str, vector: list[float], payload: dict) -> None:
        """写入（幂等）：同一个 chunk_id 重复写会覆盖，不会产生两条。"""
        self._data[chunk_id] = (vector, payload)

    def search(self, query_vec: list[float], top_k: int = 20) -> list[tuple[str, float]]:
        """检索：遍历全部向量算相似度，排序，取前 top_k 个。"""
        scored: list[tuple[str, float]] = []

        # 步骤1：逐个算分（这就是所谓的"暴力检索"，复杂度 O(N)）
        for chunk_id, (vec, _payload) in self._data.items():
            score = cosine_similarity(query_vec, vec)
            scored.append((chunk_id, score))

        # 步骤2：按分数从高到低排序
        scored.sort(key=lambda item: item[1], reverse=True)

        # 步骤3：截断前 top_k 个 —— 这就是"top-k 召回"的全部含义
        return scored[:top_k]
```

> **"top-k 召回"就是这三步**：算分 → 排序 → 切片。**没有别的**。

### 2.2 余弦相似度手算

```python
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度 = 两向量夹角余弦 = 点积 / (模长A × 模长B)。

    取值 [-1, 1]：1=完全相同方向，0=垂直（无关），-1=完全相反。
    """
    # 分子：点积 = 对应位置相乘再求和
    dot = sum(x * y for x, y in zip(a, b))

    # 分母：两个向量各自的长度
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0                       # 空向量，判定为无关
    return dot / (norm_a * norm_b)
```

**手算**：`a=[1,2]`, `b=[2,1]`
- 点积 = `1×2 + 2×1 = 4`
- `|a| = √(1+4) = 2.236`，`|b| = √(4+1) = 2.236`
- 余弦 = `4 / (2.236 × 2.236) = 4/5 = 0.8`

**如果已 L2 归一化**（§1.5）：两个模长都是 1，余弦 = 点积 = `4`。所以归一化后可以省掉开方，快很多。

### 2.3 payload 过滤怎么做的

检索时要能加条件（比如"只在采购合同里搜"）：

```python
def _matches(payload: dict, filters: dict | None) -> bool:
    """元数据过滤：filters 里每个字段都要对上，才算命中。

    filters 为 None 或空 → 不过滤，全通过。
    这是 AND 语义：{'source_type':'LAW_ARTICLE', 'contract_type':'PURCHASE'} 要求两者都满足。
    """
    if not filters:
        return True
    for key, expected in filters.items():
        if payload.get(key) != expected:
            return False          # 任一字段不匹配即淘汰
    return True
```

在 `search()` 里加一句 `if not _matches(payload, filters): continue` 即可。

### 2.4 `chunk_id` 为什么用 uuid5（幂等的关键）

```python
import uuid

_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")


def make_chunk_id(doc_id: str, section_id: str, slice_idx: int = 0) -> str:
    """用 uuid5（基于名字的确定性 UUID）生成 chunk_id。

    为什么不用 uuid4（随机）：
      随机 ID 每次重建库都变 → 同一个条款会有多个向量副本，且删除时删不干净。
      uuid5 由 (doc_id, section_id, slice_idx) 决定 → 同样的输入永远得到同样的 ID，
      所以"重复建库"是幂等的：新向量直接覆盖旧向量，不会产生孤儿数据。

    slice_idx：超长条款被切成多个窗口片时的序号（见白皮书 §3.2.1）。
    """
    # 分隔符用 '#'（与 SimpleChunker 的 "section_id#slice_idx" 风格统一）。
    # ⚠️ 严禁改动分隔符：改了会让所有既有 chunk_id 失效（存量向量全部对不上）。
    return str(uuid.uuid5(_NAMESPACE, f"{doc_id}#{section_id}#{slice_idx}"))
```

### 2.5 Qdrant 的 HNSW 解决了什么问题

暴力检索要**遍历全部 N 条**向量。算一下账：

| 语料规模 | 暴力检索（1024 维） | 感受 |
|---|---|---|
| 1,000 条 | 100 万次乘法 | 毫秒级，无感 |
| 100,000 条 | 1 亿次 | 几百毫秒，开始卡 |
| 1,000,000 条 | 10 亿次 | 秒级，**不可接受** |

**HNSW（Hierarchical Navigable Small World）** 是一种**近似最近邻（ANN）索引**：预先把向量组织成多层"小世界图"，检索时从顶层快速跳到目标附近，只比较少量候选，用**极小的精度损失**换**百倍速度**。

> Qdrant 建集合时的两个参数就是调它：
> `m=16`（每个节点连 16 条边，越大越准越占内存）、`ef_construct=200`（建索引时考察 200 个候选，越大越准越慢）。

### 2.6 对照真实类

| 玩具实现 | 真实类 | 位置 |
|---|---|---|
| `ToyVectorStore` | `InMemoryVectorStore`（dict + 暴力，零依赖） | `rag_core/adapters/inmemory_store.py` |
| `cosine_similarity` | `cosine`（纯 Python，无 numpy） | 同上 |
| `_matches` | `_matches(payload, filters)` | 同上 |
| HNSW 索引 | `QdrantVectorStore`（1024d / COSINE） | `rag_core/adapters/qdrant_store.py` |
| `make_chunk_id` | `ContractChunk.make_chunk_id` | `rag_contracts/chunk.py` |

**Qdrant 建集合的真实参数**（`qdrant_store.py`）：

```python
VectorParams(size=1024, distance=Distance.COSINE)     # 1024 维 + 余弦距离
HnswConfigDiff(m=16, ef_construct=200)                # 显式 HNSW 参数（C3 决策后补齐）
# 并对 source_type / chunk_type / contract_type / doc_id / category / law_code
# 六个字段建立 KEYWORD payload 索引，让 §2.3 的过滤走索引而非全表扫
```

> **修正记录（C3）**：本段原先只写了 `VectorParams`，未提 HNSW 参数——此前代码确实没显式设置，
> 用的是 qdrant-client 的 SDK 默认值。按"显式优于隐式"原则，已于 C3 决策后在
> `qdrant_store.py::ensure_collection` 显式传入 `HnswConfigDiff(m=16, ef_construct=200)`。
> 原因：SDK 默认值会随版本漂移，而 §2.5 的容量测算正是基于 m=16 / ef_construct=200 这组值。

---

## 第 3 章 关键词检索 BM25

### 3.1 已经有向量了，为什么还要 BM25？

因为**向量检索会漏掉精确术语**。合同场景尤其致命：

| 场景 | 向量检索 | BM25 |
|---|---|---|
| 查"违约金" | ✅ 能找到近义表述 | ✅ |
| 查条款号 `"第五百八十五条"` | ❌ 数字编号没有语义，向量几乎随机 | ✅ **精确命中** |
| 查公司名/税号 | ❌ 专有名词无语义 | ✅ **精确命中** |
| 查"不可抗力" | ✅ | ✅ |

**结论**：向量管"意思相近"，BM25 管"字面对得上"。**两者必须同时用**。

### 3.2 倒排索引

正排是"文档 → 包含哪些词"；倒排反过来，**"词 → 出现在哪些文档"**：

```python
# 倒排索引：{"违约": {"c0": 1, "c1": 1}, "试用": {"c2": 1}, ...}
```

有了它，查"违约"时不用遍历所有文档，直接查表就能拿到候选。

### 3.3 TF 与 IDF 的直觉

- **TF（词频）**：这个词在这篇文档里出现得越多 → 越相关。但**不是线性**的——出现 10 次不比出现 5 次重要一倍（边际递减）。
- **IDF（逆文档频率）**：这个词在**全部文档里越罕见** → 越有区分度。`"的"` 到处都是 → 权重接近 0；`"不可抗力"` 很少见 → 权重高。

### 3.4 BM25 公式逐项拆解

```
                    IDF(qᵢ) × f(qᵢ,D) × (k₁ + 1)
score(Q,D) = Σᵢ ─────────────────────────────────────────
                  f(qᵢ,D) + k₁ × (1 − b + b × |D| / avgdl)
```

| 符号 | 含义 | 本项目取值 | 作用 |
|---|---|---|---|
| `f(qᵢ,D)` | 词 qᵢ 在文档 D 中的出现次数 | 实测 | 出现越多分越高 |
| `\|D\|` | 文档 D 的长度（词数） | 实测 | — |
| `avgdl` | 全库平均文档长度 | 实测 | 基准线 |
| `k₁` | **饱和参数** | `1.5` | 控制 TF 的边际递减。k₁ 越大，词频越"值钱" |
| `b` | **长度归一化强度** | `0.75` | `0`=完全不惩罚长文档，`1`=完全按长度归一化 |
| `IDF` | `ln(1 + (N − df + 0.5)/(df + 0.5))` | 实测 | df=含该词的文档数，越少越值钱 |

**`b` 在干什么**：长条款天然包含更多词，容易"碰巧"命中查询词。
`b × |D| / avgdl` 这一项让长文档的分母变大 → **分数被压低**，公平性提升。

### 3.5 完整实现（逐行注释）

```python
import math
from collections import defaultdict

K1 = 1.5   # 词频饱和参数
B = 0.75   # 长度归一化强度


class ToyBM25:
    """BM25 完整实现：建索引 → 打分 → top-k。"""

    def __init__(self) -> None:
        self._tf: dict[str, dict[str, int]] = {}   # doc_id -> {词: 出现次数}
        self._doc_len: dict[str, int] = {}         # doc_id -> 词数
        self._df: dict[str, int] = defaultdict(int)  # 词 -> 有多少篇文档含它
        self._avgdl = 0.0                          # 平均文档长度

    def upsert(self, doc_id: str, tokens: list[str]) -> None:
        """建/更新索引。"""
        # 步骤1：统计词频 TF
        tf: dict[str, int] = defaultdict(int)
        for t in tokens:
            tf[t] += 1
        self._tf[doc_id] = dict(tf)
        self._doc_len[doc_id] = len(tokens)

        # 步骤2：维护 DF（每个词出现在几篇文档里）
        #        用 set(tokens) 是因为 DF 只关心"是否出现"，不关心出现几次
        for t in set(tokens):
            self._df[t] += 1

        # 步骤3：重算平均长度（每次写入后都要刷新）
        total = sum(self._doc_len.values())
        self._avgdl = total / len(self._doc_len) if self._doc_len else 0.0

    def score(self, query_tokens: list[str], doc_id: str) -> float:
        """算单个文档的 BM25 分。"""
        tf = self._tf[doc_id]
        doc_len = self._doc_len[doc_id]
        n = len(self._doc_len)
        total = 0.0

        for term in set(query_tokens):        # 查询词也去重，避免重复计分
            f = tf.get(term, 0)
            if f == 0:
                continue                      # 这个词没出现在该文档 → 贡献 0

            # IDF：df 越小（词越罕见），IDF 越大
            df = self._df.get(term, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))

            # 分母里的长度归一化项
            # 文档比平均长 → 该项 > 1 → 分母大 → 分数被压低
            norm = 1 - B + B * doc_len / (self._avgdl or 1.0)

            total += idf * f * (K1 + 1) / (f + K1 * norm)
        return total

    def search(self, query_tokens: list[str], top_k: int = 20) -> list[tuple[str, float]]:
        scored = [(doc_id, self.score(query_tokens, doc_id)) for doc_id in self._tf]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
```

**真实输出**（`QUERY="逾期付款违约金上限"`）：

```
doc c0  bm25 = 3.3432   ← 命中"逾期""付款""违约""约金"，且长度适中
doc c1  bm25 = 0.9321   ← 只命中"违约""约金"，词少分低
doc c2  bm25 = 0.0000   ← 一个查询词都没命中
```

✅ BM25 **单独就把顺序排对了**——因为它精确匹配到了"逾期""付款"这些字面词。

### 3.6 持久化与增量更新

BM25 索引在内存里，进程退出就没了。落盘用 pickle：

```python
import pickle

# 存：把三个内部表一次性序列化
with open("./data/bm25/index.pkl", "wb") as fh:
    pickle.dump((self._tf, self._doc_len, dict(self._df), self._avgdl), fh)

# 取：启动时加载
if Path("./data/bm25/index.pkl").exists():
    self._tf, self._doc_len, self._df, self._avgdl = pickle.loads(path.read_bytes())
```

**增量更新的正确姿势**——同一篇文档重新解析后，必须**先删旧的再加新的**，否则旧切片的残留词会污染 DF：

```python
def upsert(self, chunk: ContractChunk) -> None:
    self._remove(chunk.chunk_id)        # ① 先删（幂等的关键）
    # ② 再写：重新统计该 chunk 的 TF，并对 DF 中新增的词 +1（逻辑见 §3.5）
    ...
    self.save()                         # ③ 落盘

def _remove(self, chunk_id: str) -> None:
    for term in self._tf.get(chunk_id, {}):
        self._df[term] = max(0, self._df[term] - 1)   # DF 也要回退，否则 IDF 会失真
    self._tf.pop(chunk_id, None)
    self._doc_len.pop(chunk_id, None)
```

### 3.7 对照真实类

| 玩具实现 | 真实类 | 位置 |
|---|---|---|
| `ToyBM25` | `BM25Store`（实现 `SparseStorePort`） | 白皮书 §3.2.2 |
| `upsert/_remove/save` | `BM25Store.upsert / _remove / save`（pickle 落盘） | 同上 |
| — | `SparseStorePort` 协议（upsert/search/delete_by_doc/health） | `rag_contracts/ports.py` |

---

## 第 4 章 两路结果怎么合并（RRF）

### 4.1 为什么不能直接把两个分数相加？

**量纲完全不同**：

```
dense（余弦）:  c0 = 0.3467    ← 范围 [-1, 1]
sparse（BM25）: c0 = 3.3432    ← 范围 [0, +∞)，长文档能到几十
```

直接相加的话，BM25 的原始分**完全压过**余弦分，等于"稠密检索白做了"。而且 BM25 的分还会随语料规模漂移，没法定阈值。

### 4.2 RRF 的思路：不看分数，只看名次

**RRF（Reciprocal Rank Fusion，倒数排名融合）** 丢弃原始分数，只用**排名**：

```
score(d) = Σ_各路  1 / (k + rank_路(d))
```

- `rank` 从 1 开始（第 1 名）
- `k = 60` 是**平滑常数**：让第 1 名和第 2 名的差距不至于过大，避免单路结果"一言堂"
- 某路没召回该文档 → 该项不贡献（自然获得最低权重）

**为什么有效**：名次是**可比的**（都是 1、2、3…），与量纲无关。

### 4.3 完整实现

```python
def rrf_fuse(ranked_lists: list[list[tuple[str, float]]], k: int = 60) -> list[tuple[str, float]]:
    """RRF 融合：把多路有序结果合并成一路。

    入参: ranked_lists —— 每路是 [(chunk_id, 原始分), ...]，必须已按分数降序
          k           —— 平滑常数，项目默认 60
    出参: [(chunk_id, rrf分), ...]，按 rrf 分降序
    """
    fused: dict[str, float] = {}

    for ranked in ranked_lists:
        # enumerate(start=1)：让 rank 从 1 开始（第一名是 1，不是 0）
        for rank, (chunk_id, _original_score) in enumerate(ranked, start=1):
            # 原始分数 _original_score 在这里被完全丢弃 —— 这是 RRF 的核心
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)

    return sorted(fused.items(), key=lambda item: item[1], reverse=True)
```

**注意那个 `_original_score`**：变量以下划线开头，意思是"故意不用"。**丢掉分数、只留名次**，正是 RRF 能跨量纲融合的原因。

### 4.4 两个真实场景（实测数字）

**场景 A：两路看法一致**（正常情况）

```
dense : c0=0.3467(第1)  c1=0.2372(第2)  c2=0.0(第3)
sparse: c0=3.3432(第1)  c1=0.9321(第2)  c2=0.0(第3)
→ RRF: c0 = 1/61 + 1/61 = 0.032787    ← 两路都第1，最高
       c1 = 1/62 + 1/62 = 0.032258
       c2 = 1/63 + 1/63 = 0.031746
```

**场景 B：两路分歧**（dense 排错、sparse 排对）——**这才是 RRF 的价值**

```
dense : c1=0.44(第1)  c0=0.35(第2)  c2=0.0(第3)     ← 稠密检索错了
sparse: c0=3.3432(第1)  c2=0.0(第2)                  ← 关键词检索对了
→ RRF: c0 = 1/62 + 1/61 = 0.032522    ← 被 sparse 拉回第1 ✅
       c2 = 1/63 + 1/62 = 0.032002
       c1 = 1/61       = 0.016393    ← 只在一路出现，权重直接腰斩
```

**看懂了吗**：`c1` 只在 dense 里排第 1，但 sparse 完全没召回它 → 只拿到一份权重，被 `c0`（两路都有）超越。**RRF 天然实现了"多路投票"**。

### 4.5 对照真实类

| 玩具实现 | 真实函数 | 位置 |
|---|---|---|
| `rrf_fuse` | `rrf_fuse(ranked_lists, k=60)` | 白皮书 §3.2.3（待建 `rag_core/fusion.py`） |
| `k=60` | `DEFAULT_RRF_K = 60` / `CG_RAG_RRF_K` | `rag_contracts/query.py` |
| RRF 分 | `ChunkScores.fused` | `rag_contracts/query.py` |

> ⚠️ 现状提醒：**`fused` 目前恒为 `None`**，因为 RRF 尚未接入（白皮书 §5 列为 T03 待建）。

---

## 第 5 章 精排 Rerank

### 5.1 最核心的区别：双塔 vs 交叉编码

这是理解 RAG 的**分水岭**：

| | 双塔 / Bi-Encoder（向量召回用） | 交叉编码 / Cross-Encoder（精排用） |
|---|---|---|
| 怎么算 | query 和文档**各自独立**编码成向量，再算余弦 | query 和文档**拼在一起**送进模型，直接输出一个分数 |
| 输入 | `encode(q)`、`encode(d)` 分开 | `model(q, d)` 一起 |
| 能否提前算 | ✅ 文档向量**离线算好存起来** | ❌ 必须**在线**对每个 (q,d) 对实时算 |
| 速度 | 极快（百万级毫秒） | 慢（每次都要跑一遍大模型） |
| 精度 | 一般（压缩成向量时丢了细节） | **高**（能看到 query 和文档的逐词交互） |

```
双塔:   "违约金上限" → [0.2,-0.1,...]  ┐
                                        ├→ 余弦 0.35
        "违约金不超过10%" → [0.3,0.0,.] ┘

交叉:   "[CLS] 违约金上限 [SEP] 违约金不超过10% [SEP]" → 模型 → 0.87
```

**交叉编码能"看到"两个文本放在一起时的交互**（比如"上限"和"不超过10%"的对应关系），所以更准。

### 5.2 为什么必须"先召回、后精排"？算一笔账

假设 100 万条条款：

| 方案 | 计算量 | 可行性 |
|---|---|---|
| 直接对全库精排 | 100 万次 Cross-Encoder 推理 | ❌ 几小时，不可能 |
| **先向量召回 Top-20，再精排** | 1 次向量检索（毫秒）+ **20 次**精排 | ✅ 几百毫秒 |

**这就是"召回 → 精排"漏斗的全部动机**：用便宜的方法把候选从百万缩到 20，再用昂贵的方法精挑。

### 5.3 完整实现（HTTP 版，规避 torch 依赖）

```python
import httpx


class ToyReranker:
    """Cross-Encoder 精排（走 HTTP 服务，本地不装 torch）。

    输入：query + N 个候选文本
    输出：N 个 [0,1] 的相关性分数
    """

    def __init__(self, url: str, model: str = "BAAI/bge-reranker-v2-m3",
                 timeout_ms: int = 3000) -> None:
        self._url = url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_ms / 1000.0

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """一次请求把所有 (query, text) 对送进去打分。"""
        if not texts:
            return []                                   # 空输入直接返回，不发无效请求

        # 步骤1：组 payload —— 一条 query 配一批候选，让服务端批量推理
        payload = {"model": self._model, "query": query, "texts": texts}

        try:
            # 步骤2：发请求。注意超时是硬约束，宁可失败也不能拖垮整条链路
            resp = httpx.post(f"{self._url}/rerank", json=payload, timeout=self._timeout_s)
            resp.raise_for_status()                     # 非 2xx 直接抛异常
            body = resp.json()
        except Exception as exc:
            # 步骤3：失败语义 —— 抛「可降级异常」，交给上层退回召回分数
            #        绝不能让精排失败导致整个检索失败（见第 8 章）
            raise RerankFailedError(f"rerank 失败: {exc}") from exc

        scores = body.get("scores")
        if not isinstance(scores, list) or len(scores) != len(texts):
            raise RerankFailedError("rerank 返回结构不合法")   # 防御：条数必须对上

        # 步骤4：clamp 到 [0,1]（见 5.5 的红线）
        return [max(0.0, min(1.0, float(s))) for s in scores]
```

### 5.4 Top-20 → Top-3~5 的截断逻辑

```python
def apply_rerank(query: str, candidates: list, reranker, top_n: int = 5) -> list:
    """精排并截断。candidates 已按召回分排好序。"""
    # 步骤1：只取候选的文本送进去打分（候选一般 20 条）
    texts = [c.text for c in candidates]
    scores = reranker.rerank(query, texts)

    # 步骤2：把分数写回每个候选对象
    for cand, score in zip(candidates, scores):
        cand.rerank_score = score

    # 步骤3：按精排分重新排序 —— 注意这里的顺序可能和召回顺序完全不同
    candidates.sort(key=lambda c: c.rerank_score, reverse=True)

    # 步骤4：截断到最终条数（3~5 条，够 LLM 用又不超预算）
    return candidates[:top_n]
```

### 5.5 🔴 阈值 0.35 与 clamp 红线

精排分 `final` 低于 `score_threshold=0.35` 的候选会被**丢弃**：

```python
kept = [d for d in docs if d.scores.final >= request.score_threshold]
```

**为什么 0.35 这么重要**：它同时是"**缺失条款检测**"的触发线。全部候选都低于 0.35 → 说明**标准条款库里根本没有可对照的范本** → 触发 `EMPTY_RECALL` → 报告输出"缺失标准对照，建议补全"。**这是赛题明确要求的能力。**

**为什么必须用 clamp 而不是 `(x+1)/2`**（白皮书红线 1，源码注释级）：

```python
# ❌ 错误做法：把余弦 [-1,1] 线性映射到 [0,1]
final = (cosine + 1) / 2
# 后果：一段完全无关的文本 cosine≈0 → final = 0.5
#       0.5 > 0.35 阈值 → 被判定为"找到了对照条款"
#       → 缺失检测彻底失灵，所有垃圾都能通过

# ✅ 正确做法：clamp（截断负数到 0，保留正值原样）
final = max(0.0, min(1.0, cosine))
# 无关文本 cosine≈0 → final = 0.0 < 0.35 → 正确判定为"无对照"
```

### 5.6 对照真实类

| 玩具实现 | 真实类 | 位置 |
|---|---|---|
| `ToyReranker` | `HttpReranker`（实现 `RerankerPort`） | 白皮书 §3.2.4（待建） |
| `apply_rerank` | `RAGService._rank()` 中的截断逻辑 | `rag_core/service.py` |
| 恒等透传 | `NoopReranker`（**不是**全打 0，是原样透传召回分） | `rag_core/adapters/noop_rerank.py` |
| `0.35` | `DEFAULT_SCORE_THRESHOLD` / `CG_RAG_RERANK_THRESHOLD` | `rag_contracts/query.py` |

---

## 第 6 章 上下文组装与 Token 预算

### 6.1 为什么要预算

LLM 的输入窗口有限，且按 token 计费。检索到 20 条不能全塞进去——要按预算**贪心填充**。

### 6.2 完整实现

```python
import math


def estimate_tokens(text: str) -> int:
    """Token 估算：中文 1 个 token ≈ 1.5 个字符。

    为什么是 1.5：中文字符在主流分词器里大约 1~2 个 token，
    取 1.5 是保守偏高的估计（估高了只会少塞内容，不会超预算）。
    装了 tiktoken 后项目会自动切换为精确计数（TokenCounter.backend == 'tiktoken'）。
    """
    return math.ceil(len(text) / 1.5)


def build_context(docs: list, budget: int = 3000) -> tuple[str, int, bool]:
    """贪心填充上下文。

    返回: (拼好的文本, 实际token数, 是否被截断)
    """
    blocks: list[str] = []
    used = 0
    truncated = False

    for index, doc in enumerate(docs, start=1):
        cost = estimate_tokens(doc.text)

        # 关键设计：已经装了至少一条后，装不下就停
        #           但【第一条永远要装】——哪怕超预算，否则 LLM 什么都看不到
        if used + cost > budget and blocks:
            truncated = True
            continue                     # 跳过这条，继续尝试后面更短的

        blocks.append(f"[{index}] ({doc.source_type}) {doc.title}\n{doc.text}")
        used += cost

    if not blocks:
        # 空召回时不能返回空字符串，要返回提示语，否则 LLM 会瞎编
        return EMPTY_CONTEXT_HINT, 0, False

    return "\n\n".join(blocks), used, truncated
```

### 6.3 真实输出（预算 30 token 的迷你演示）

```
组装上下文: chunks=2 tokens=30/30
   [1] c0 (17tok) 买方逾期付款的，应按日支付合同总价千分之...
   [2] c1 (13tok) 违约金总额累计不超过合同总价的百分之十...
```

### 6.4 `EMPTY_CONTEXT_HINT` 为什么必须原样传给 LLM

空召回时如果返回空字符串，LLM 会因为"没有依据"而**幻觉编造**。项目里写死了一句提示：

> 知识库未检索到可对照的标准条款或法条，请依据通用法律原则评估，并在结论中明确标注【缺失标准对照】。

这句话同时完成两件事：① 告诉模型"没有依据，别硬编"；② 要求模型**在输出里显式标注缺失**——这正是可解释性的要求。

### 6.5 对照真实类

| 玩具实现 | 真实类 | 位置 |
|---|---|---|
| `estimate_tokens` | `TokenCounter.count`（1.5 字符/token，支持 tiktoken） | `cg_observability/token_counter.py` |
| `build_context` | `ContextBuilder.build` → `ContextBuildResult` | `rag_core/context_builder.py` |
| 分阶段计时 | `TokenMeter.stage("embed")` 上下文管理器 | `cg_observability/token_counter.py` |

---

## 第 7 章 溯源 Citation

### 7.1 为什么要单独做一层

LLM 输出的风险结论必须能**点回原文**（赛题"可解释性"硬要求）。召回的 chunk 有 28 个字段，但前端只需要 4 个——`to_citation` 就是这层"降维映射"。

### 7.2 完整实现

```python
MAX_CONTENT_CHARS = 800


def to_citation(chunk, score: float, max_chars: int = MAX_CONTENT_CHARS) -> dict:
    """把 28 字段的 ContractChunk 压成 4 字段的溯源对象（与 PreciseCitation 1:1）。"""
    return {
        # 来源类型：STANDARD_CLAUSE / LAW_ARTICLE / PRECEDENT（三值冻结，禁止加第四值）
        "sourceType": chunk.source_type.value,

        # 展示标题：有 title 用 title；没有就依次退化
        #   law_title+article_no → law_title → section_path → "doc_id#section_id"
        "sourceTitle": chunk.citation_title(),

        # 原文片段：截断到 800 字并加省略号，避免超长撑爆前端与 token 预算
        "contentChunk": truncate_text(chunk.text, max_chars),

        # 相关性分数：clamp 到 [0,1]，NaN 归 0
        "relevanceScore": clamp_score(score),
    }


def truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"          # 用单字符省略号，明确表示"被截断了"


def clamp_score(score: float) -> float:
    """分数规整：NaN → 0.0，超出 [0,1] → 截断到边界。

    NaN 必须处理：某些 reranker 在异常时会返回 NaN，
    NaN 参与比较永远为 False，会导致该条既不被保留也不被丢弃的诡异状态。
    """
    if score != score:                  # NaN 的唯一判据：自己不等于自己
        return 0.0
    return max(0.0, min(1.0, score))
```

### 7.3 对照真实类

| 玩具实现 | 真实函数/类 | 位置 |
|---|---|---|
| `to_citation` | `to_citation(chunk, score)` | `rag_contracts/citation.py` |
| `truncate_text` / `clamp_score` | 同名函数 | 同上 |
| 输出对象 | `CitationOut`（**唯一 camelCase 模型**） | `rag_contracts/query.py` |
| 前端/报告消费 | `PreciseCitation`（SSOT 生成） | `openapi.yaml` L229-236 |

> ⚠️ **这是全项目唯一一处 camelCase 契约**，因为要直接对齐 SSOT 的 `PreciseCitation`。其余模型全是 snake_case。写错大小写会直接校验失败。

---

## 第 8 章 降级与异常处理：为什么"永不抛异常"

### 8.1 设计哲学

RAG 是**增强**模块，**不是主链路**。向量库挂了、精排超时了，系统应该：

- ❌ 不能让整个合同审核失败
- ✅ 应该返回"检索能力降级"的结果，让 LLM 在没有外部依据的情况下继续工作

这就是 `degraded=True` + `fallback_reason` 的意义。

### 8.2 完整状态机实现

```python
def retrieve_safely(service, request):
    """永不抛异常的检索包装：任何失败都转成降级响应。"""
    warnings: list[str] = []

    try:
        return service.retrieve(request)

    except RagTimeoutError:
        # 超时：可重试类型，但已经重试过了 → 降级
        warnings.append("检索超时，已降级为无外部依据模式")
        return degraded_response(request, reason=FallbackReason.TIMEOUT, warnings=warnings)

    except StoreUnavailableError:
        # 向量库不可用 → 降级
        warnings.append("向量库不可用，已降级")
        return degraded_response(request, reason=FallbackReason.STORE_UNAVAILABLE, warnings=warnings)

    except RagError as exc:
        # 其它已知 RAG 错误 → 统一归到内部错误
        warnings.append(f"检索失败: {exc.code}")
        return degraded_response(request, reason=FallbackReason.STORE_UNAVAILABLE, warnings=warnings)

    except Exception as exc:
        # 兜底：未知异常也绝不上抛
        warnings.append("检索出现未知异常，已降级")
        return degraded_response(request, reason=FallbackReason.STORE_UNAVAILABLE, warnings=warnings)
```

### 8.3 超时重试与指数退避

```python
import time


def call_with_retry(fn, max_retries: int = 3):
    """指数退避重试：0.1s → 0.2s → 0.4s。

    只对 TransientRagError（retryable=True）重试：
      - 网络抖动、超时 → 重试有意义
      - 参数非法、契约校验失败 → 重试一万次也一样失败，直接放弃
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except TransientRagError:
            if attempt == max_retries:
                raise                          # 次数耗尽，交给上层降级
            time.sleep(0.1 * (2 ** (attempt - 1)))   # 2^(n-1) → 0.1/0.2/0.4s
```

**为什么是指数退避**：服务如果是瞬时过载，立刻重试会**雪上加霜**；等待时间指数增长，给服务恢复的窗口。

### 8.4 对照真实类

| 玩具实现 | 真实实现 | 位置 |
|---|---|---|
| `retrieve_safely` | `RAGService.retrieve` 内的 try/except + `_degraded()` | `rag_core/service.py` |
| `FallbackReason` | 7 值枚举（NONE/OVERLONG_QUERY/EMPTY_RECALL/TIMEOUT/STORE_UNAVAILABLE/RERANK_FAILED/EMBED_FAILED） | `rag_contracts/enums.py` |
| `call_with_retry` | 白皮书 §3.3.1 `build_rag_tool` 内的重试循环 | — |
| 错误码 | `CG-RAG-1001~3002`（**内部码，不外泄**） | `rag_contracts/errors.py` |

> 🔑 **`ERR_EMPTY_RECALL` 不是错误**：它是"握手信号"，表示"知识库确实没有对照条款"，走 `degraded=true` 正常返回，**不抛异常、不告警**。

---

## 第 9 章 Agent Tool-calling：到底发生了什么

### 9.1 四步流程

```
① 你把函数的 JSON Schema 告诉 LLM
      ↓
② LLM 判断"这个问题需要查知识库"，输出一个调用请求 JSON
      {"name":"retrieve_contract_knowledge","arguments":{"query":"违约金上限","top_k":5}}
      ↓
③ 你的代码解析这个 JSON，真正执行函数，拿到结果字符串
      ↓
④ 把结果字符串追加进对话，让 LLM 基于它生成最终回答
```

**关键认知**：LLM **不执行任何代码**。它只是"输出一段表示我要调用什么"的文本，真正执行的是你的 Python。

### 9.2 `build_function_calling_spec()` 生成的东西长什么样

```json
{
  "type": "function",
  "function": {
    "name": "retrieve_contract_knowledge",
    "description": "从企业标准条款库、《民法典》等法律法规库与裁判文书库中检索与给定条款相关的...",
    "strict": true,
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      // ⚠️ strict:true 模式下所有字段都必须在 required 里（OpenAI 硬约束，否则 400）。
      // 真实生成逻辑：params["required"] = sorted(properties.keys())
      // 所以这里是「全部 5 个字段」而非只有 query。示例省略了 source_types / reason。
      "required": ["contract_type", "query", "reason", "source_types", "top_k"],
      "properties": {
        "query": { "type": "string", "minLength": 1, "maxLength": 2000 },
        "contract_type": { "enum": ["PURCHASE","SALES","NDA","OUTSOURCING","LABOR","OTHER"] },
        "top_k": { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 }
      }
    }
  }
}
```

**`description` 极其重要**——LLM 完全靠这段自然语言决定"什么时候该调用这个工具"。写得含糊，模型就不会调用。

### 9.3 完整实现（含三个边界处理）

```python
def retrieve_contract_knowledge(query: str, contract_type: str | None = None,
                                top_k: int = 5) -> str:
    """工具主体。返回【字符串】，因为 LLM 只能消费文本。"""
    warnings: list[str] = []
    fallback = FallbackReason.NONE

    # ── 边界 1：超长文本拦截 ──────────────────────────────
    # 原因：embedding 服务有长度上限，超长会被拒绝或截断得面目全非
    if len(query) > MAX_QUERY_CHARS:              # 2000
        query = query[:MAX_QUERY_CHARS]           # 硬截断（前端也应提示用户）
        warnings.append("query 过长已截断")
        fallback = FallbackReason.OVERLONG_QUERY

    # ── 边界 3：超时重试（见 8.3）──────────────────────────
    try:
        response = call_with_retry(lambda: service.retrieve(RAGQueryRequest(
            query=query, top_k=top_k, filters=_build_filters(contract_type),
        )))
    except RagError as exc:
        # ── 重试仍失败 → 降级，绝不把异常抛给 Agent ──────────
        return AgentRAGToolResult(
            status="DEGRADED",
            context_block=EMPTY_CONTEXT_HINT,     # 用提示语占位，让 LLM 知道没依据
            citations=[], token_usage=TokenUsage(),
            warnings=[f"检索失败: {exc.code}"],
            fallback_reason=FallbackReason.STORE_UNAVAILABLE,
        ).to_tool_message()

    # ── 边界 2：空召回降级 ────────────────────────────────
    if not response.retrieved_docs:
        return AgentRAGToolResult(
            status="EMPTY",                        # 三态之一
            context_block=EMPTY_CONTEXT_HINT,
            citations=[], trace_id=response.trace_id,
            token_usage=response.token_usage,
            fallback_reason=FallbackReason.EMPTY_RECALL,
        ).to_tool_message()

    # ── 正常返回 ──────────────────────────────────────────
    return AgentRAGToolResult(
        status="OK",
        context_block=response.context_block,      # 已拼好的检索上下文
        citations=[d.citation for d in response.retrieved_docs],  # 供报告溯源
        trace_id=response.trace_id,
        token_usage=response.token_usage,
        fallback_reason=FallbackReason.NONE,
    ).to_tool_message()
```

**`to_tool_message()` 的输出形态**（这才是真正喂给 LLM 的文本）：

```
[status=OK][trace_id=9f1c2b...]
[1] (STANDARD_CLAUSE) 第六条 逾期付款违约金 [section=第六条]
买方逾期付款的，应按日支付合同总价千分之五的违约金，违约金总额累计不超过合同总价的百分之十。
[2] (LAW_ARTICLE) 第五百八十五条 违约金 [section=第五百八十五条]
当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金……
```

### 9.4 对照真实类

| 玩具实现 | 真实类/函数 | 位置 |
|---|---|---|
| 工具函数 | `build_rag_tool(service)` 返回的 `@tool` 对象 | 白皮书 §3.3.1 |
| 入参模型 | `AgentRAGToolInput`（query/top_k/contract_type/source_types/reason） | `rag_contracts/agent_tool.py` |
| 出参模型 | `AgentRAGToolResult`（status 三态 OK/DEGRADED/EMPTY） | 同上 |
| Schema 生成 | `build_function_calling_spec()` | 同上 |
| 图编排 | LangGraph `StateGraph`（router→retrieve→generate） | 白皮书 §3.3.2 |

---

## 第 10 章 端到端：把上面所有零件串起来（可复制直接运行）

下面这个脚本**只用标准库**，实现了完整的 RAG 流程。复制保存为 `mini_rag.py` 直接 `python mini_rag.py` 即可运行，每一步都会打印中间结果。

```python
"""mini_rag.py —— 60 行看懂整个 RAG 检索链路（纯标准库，可直接运行）。"""
import hashlib
import math

DIM = 1024          # 向量维度（项目锁定 1024）
K1, B = 1.5, 0.75   # BM25 参数
RRF_K = 60          # RRF 平滑常数
THRESHOLD = 0.05    # 本演示用小阈值（真实项目 0.35）
BUDGET = 30         # token 预算（真实项目 3000）

CORPUS = {
    "c0": "买方逾期付款的，应按日支付合同总价千分之五的违约金",
    "c1": "违约金总额累计不超过合同总价的百分之十",
    "c2": "试用期最长不得超过六个月",
}
QUERY = "逾期付款违约金上限"


# ── 零件1：切词（中文二元组）─────────────────────────────
def tokenize(text):
    return [text[i:i + 2] for i in range(len(text) - 1)]


# ── 零件2：向量化（hashing trick + L2 归一化）────────────
def embed(text):
    vec = [0.0] * DIM
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        vec[int.from_bytes(digest, "big") % DIM] += 1.0 if (digest[0] & 1) else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


# ── 零件3：余弦相似度（已归一化 → 等价于点积）─────────────
def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


# ── 零件4：BM25 ─────────────────────────────────────────
class BM25:
    def __init__(self, docs):
        self.tf = {k: self._count(v) for k, v in docs.items()}
        self.len = {k: len(v) for k, v in docs.items()}
        self.avgdl = sum(self.len.values()) / len(self.len)
        self.df = {}
        for tokens in docs.values():
            for t in set(tokens):
                self.df[t] = self.df.get(t, 0) + 1

    @staticmethod
    def _count(tokens):
        tf = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        return tf

    def score(self, q_tokens, doc_id):
        tf, n, total = self.tf[doc_id], len(self.tf), 0.0
        for term in set(q_tokens):
            f = tf.get(term, 0)
            if not f:
                continue
            df = self.df.get(term, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            total += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * self.len[doc_id] / self.avgdl))
        return total


# ── 零件5：RRF 融合 ─────────────────────────────────────
def rrf(lists, k=RRF_K):
    fused = {}
    for ranked in lists:
        for rank, (cid, _score) in enumerate(ranked, 1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: -x[1])


# ── 主流程 ──────────────────────────────────────────────
if __name__ == "__main__":
    # ① 建库：每篇文档算一个向量
    vectors = {cid: embed(text) for cid, text in CORPUS.items()}
    tokenized = {cid: tokenize(text) for cid, text in CORPUS.items()}
    print(f"建库完成: {len(vectors)} 条, 维度 {DIM}")

    # ② 稠密召回
    qv = embed(QUERY)
    dense = sorted(((c, cosine(qv, v)) for c, v in vectors.items()), key=lambda x: -x[1])
    print("dense :", [(c, round(s, 4)) for c, s in dense])

    # ③ 稀疏召回
    bm25 = BM25(tokenized)
    sparse = sorted(((c, bm25.score(tokenize(QUERY), c)) for c in CORPUS), key=lambda x: -x[1])
    print("sparse:", [(c, round(s, 4)) for c, s in sparse])

    # ④ RRF 融合
    fused = rrf([dense, sparse])
    print("fused :", [(c, round(s, 6)) for c, s in fused])

    # ⑤ 阈值过滤
    kept = [c for c, _ in fused if cosine(qv, vectors[c]) >= THRESHOLD]
    print(f"阈值{THRESHOLD}后:", kept)

    # ⑥ 上下文组装（贪心填充）
    used, ctx = 0, []
    for c in kept:
        text = CORPUS[c]
        cost = math.ceil(len(text) / 1.5)
        if used + cost > BUDGET and ctx:
            continue
        ctx.append((c, text, cost))
        used += cost
    print(f"上下文: chunks={len(ctx)} tokens={used}/{BUDGET}")
    for i, (c, t, n) in enumerate(ctx, 1):
        print(f"  [{i}] {c} ({n}tok) {t}")
```

**真实运行输出**：

```
建库完成: 3 条, 维度 1024
dense : [('c0', 0.3467), ('c1', 0.2372), ('c2', 0.0)]
sparse: [('c0', 3.3432), ('c1', 0.9321), ('c2', 0.0)]
fused : [('c0', 0.032787), ('c1', 0.032258), ('c2', 0.031746)]
阈值0.05后: ['c0', 'c1']
上下文: chunks=2 tokens=30/30
  [1] c0 (17tok) 买方逾期付款的，应按日支付合同总价千分之五的违约金
  [2] c1 (13tok) 违约金总额累计不超过合同总价的百分之十
```

✅ 最终结果：`c2`（试用期）被正确淘汰，`c0`/`c1` 按预算塞进上下文。**这就是整个 RAG 检索的全部**。

---

## 附录：名词 → 代码位置速查

| 你听到的名词 | 本质是什么 | 本文章节 | 项目真实位置 |
|---|---|---|---|
| **向量化 / Embedding** | 把文本变成一串数字（本项目 1024 个） | §1 | `rag_core/adapters/embedding.py` |
| **Hashing Trick** | 不用模型，靠哈希把词"扔"进向量位置 | §1.3 | `HashEmbedder` |
| **L2 归一化** | 把向量长度缩到 1，只留方向 | §1.5 | 内置于 `embed_one` |
| **向量库** | 存向量的地方，本质是 dict + 相似度计算 | §2.1 | `InMemoryVectorStore` / `QdrantVectorStore` |
| **余弦相似度** | 点积 ÷ 两个模长，衡量"方向是否一致" | §2.2 | `cosine()` |
| **top-k 召回** | 算分 → 排序 → 切前 k 个，**仅此而已** | §2.1 步骤3 | `VectorStorePort.search(top_k)` |
| **ANN / HNSW** | 近似索引，用极小精度损失换百倍速度 | §2.5 | `QdrantVectorStore`（m=16, ef_construct=200） |
| **payload / 元数据过滤** | 检索时附加的 AND 条件 | §2.3 | `_matches(payload, filters)` |
| **BM25** | 关键词打分：词频 × 稀有度 ÷ 长度惩罚 | §3.4 | `BM25Store`（T03 待建） |
| **倒排索引** | 词 → 出现在哪些文档 | §3.2 | `BM25Store._df` |
| **TF / IDF** | 出现次数 / 稀有程度 | §3.3 | `BM25Store.score()` |
| **RRF 融合** | 丢掉分数只用名次，`Σ1/(k+rank)` | §4.2 | `rrf_fuse()`（T03 待建） |
| **精排 / Rerank** | Cross-Encoder 逐对 (query, doc) 重新打分 | §5.3 | `HttpReranker`（T03 待建） |
| **双塔 vs 交叉编码** | 分开编码 vs 拼一起编码（精度/速度权衡） | §5.1 | — |
| **score_threshold=0.35** | 相关性门槛，同时是"缺失检测"触发线 | §5.5 | `DEFAULT_SCORE_THRESHOLD` |
| **Token 预算** | 按预算贪心塞上下文，至少塞一条 | §6.2 | `ContextBuilder.build` |
| **Citation 溯源** | 28 字段 chunk → 4 字段引用对象 | §7.2 | `to_citation()` |
| **degraded / fallback** | 检索失败不抛异常，返回降级结果 | §8 | `RAGService._degraded()` |
| **Tool-calling** | LLM 输出调用意图 JSON，你的代码真正执行 | §9.1 | `build_rag_tool()` |
| **Function Calling Spec** | 告诉 LLM"有哪些工具、参数是什么"的 JSON | §9.2 | `build_function_calling_spec()` |

---

## 常见疑问速答

**Q：为什么不直接用向量检索，要搞两路？**
A：向量检索对"第五百八十五条"这类编号、公司名、税号几乎无能为力（无语义），BM25 精确匹配能补上。见 §3.1。

**Q：为什么先召回 20 条再精排到 5 条，不直接精排全部？**
A：精排要跑大模型，100 万条要几小时；先召回缩到 20 条只要毫秒级。见 §5.2 的算账。

**Q：`HashEmbedder` 和 bge-m3 差在哪？**
A：HashEmbedder 只是词面哈希，**没有语义**——"逾期付款"和"延迟支付"它认为无关。bge-m3 是神经网络，能理解同义。所以 `phase1_smoke` 会有空召回，生产必须切 `CG_RAG_EMBED_MODE=http`。见 §1.7。

**Q：为什么 `fused` 字段是 None？**
A：RRF 融合尚未接入（T03 待建）。当前只有 dense 路径。见 §4.5。

**Q：0.35 这个数是怎么定的？**
A：经验阈值，兼顾"不漏掉真对照"和"不引入噪声"。它的关键作用是**缺失检测**的判定线，改小会导致误判为"有对照"，改大会导致漏检。见 §5.5。
