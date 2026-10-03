"""要素 key 词表（**唯一处**，C80 / T-D8-5）。

设计要点：
1. **唯一处**：解析层（`doc_processor._ELEMENT_RULES`）、审核层（`ai_engine.extract_node`）、
   prompt 模板与评测脚本一律引用本模块，禁止各写一份 key 清单。
2. **纯加法**：本模块不修改 `rag_contracts` 既有字段/枚举，也不改 `openapi.yaml`
   （`ContractElement.key` 仍是自由 string）。
3. **与既有契约对齐**：`ContractElement.isCustom` 的语义固定为
   「核心词表 → false；明细白名单 → true」，与解析层 `ParsedElement.is_custom=False` 一致。
4. **消重**：与核心同义的旧 key（如 `contract_amount`）走别名归一到核心，
   不再另立同概念明细。

背景：C79 实测要素 micro-F1 = 2.47%，根因是审核层 LLM 自由发明 key
（`contract_amount` / `contract_start_date` / `contract_quantity` …）与解析层固定 6 key 脱节。
"""

from __future__ import annotations

#: 核心要素（有序）：参与赛题「关键要素抽取 F1」指标，`isCustom` 必须为 False。
CORE_ELEMENT_KEYS: tuple[tuple[str, str], ...] = (
    ("party_a", "甲方"),
    ("party_b", "乙方"),
    ("amount", "合同金额"),
    ("sign_date", "签订日期"),
    ("term", "合同期限"),
    ("jurisdiction", "管辖地"),
)

#: 明细要素白名单：仅展示、不计入核心 F1，`isCustom` 为 True。
DETAIL_ELEMENT_KEYS: tuple[tuple[str, str], ...] = (
    ("contract_subject_matter", "合同标的"),
    ("contract_quantity", "数量"),
    ("goods_model", "规格型号"),
    ("price_type", "价格类型"),
    ("payment_method", "付款方式"),
    ("contract_start_date", "合同起始日"),
    ("contract_end_date", "合同终止日"),
    ("breach_liability", "违约责任"),
)

#: 别名 → 核心 key：与核心同义的旧 key 一律归一到核心，避免同概念两处。
ELEMENT_KEY_ALIASES: dict[str, str] = {
    "contract_amount": "amount",
}

#: 核心 key 集合（判定用）。
_CORE_KEY_SET: frozenset[str] = frozenset(key for key, _ in CORE_ELEMENT_KEYS)

#: 明细 key 集合（判定用）。
_DETAIL_KEY_SET: frozenset[str] = frozenset(key for key, _ in DETAIL_ELEMENT_KEYS)

#: 允许出现的全部 key（核心 ∪ 明细）。
ALLOWED_ELEMENT_KEYS: frozenset[str] = _CORE_KEY_SET | _DETAIL_KEY_SET

#: 核心 key → 中文标签（展示/提示词复用）。
CORE_ELEMENT_LABELS: dict[str, str] = dict(CORE_ELEMENT_KEYS)

#: 明细 key → 中文标签。
DETAIL_ELEMENT_LABELS: dict[str, str] = dict(DETAIL_ELEMENT_KEYS)


def normalize_key(key: object) -> str:
    """归一 key：去空白、转小写、按别名映射到核心 key；非法输入返回空串。"""
    if key is None:
        return ""
    text = str(key).strip().lower()
    if not text:
        return ""
    return ELEMENT_KEY_ALIASES.get(text, text)


def is_core_key(key: object) -> bool:
    """归一后是否为核心词表 key（决定 `isCustom=False`）。"""
    return normalize_key(key) in _CORE_KEY_SET


def is_allowed_key(key: object) -> bool:
    """归一后是否在允许词表（核心 ∪ 明细）内。"""
    return normalize_key(key) in ALLOWED_ELEMENT_KEYS


def is_detail_key(key: object) -> bool:
    """归一后是否为明细白名单 key（`isCustom=True`）。"""
    return normalize_key(key) in _DETAIL_KEY_SET


def label_for(key: object, *, default: str = "") -> str:
    """取该 key 的中文标签（核心/明细通查）；未知返回 `default`。"""
    normalized = normalize_key(key)
    return CORE_ELEMENT_LABELS.get(normalized) or DETAIL_ELEMENT_LABELS.get(
        normalized, default
    )


def build_prompt_block() -> str:
    """构造给抽取提示词注入的 key 清单文本（**唯一处**，模板只放占位符）。"""
    core = "\n".join(f"- {key}（{label}）[核心，is_custom=false]" for key, label in CORE_ELEMENT_KEYS)
    detail = "\n".join(
        f"- {key}（{label}）[明细，is_custom=true]" for key, label in DETAIL_ELEMENT_KEYS
    )
    aliases = "、".join(f"{old}→{new}" for old, new in ELEMENT_KEY_ALIASES.items())
    return (
        "允许的 key 清单（**只能从中选取，不得发明新 key**）：\n"
        f"{core}\n{detail}\n"
        f"（另有旧别名：{aliases}，请直接使用归一后的 key）"
    )


#: 提示词注入块的缓存值（模板占位符 `{{element_keys}}` 的取值）。
ELEMENT_KEYS_PROMPT_BLOCK: str = build_prompt_block()


__all__ = [
    "ALLOWED_ELEMENT_KEYS",
    "CORE_ELEMENT_KEYS",
    "CORE_ELEMENT_LABELS",
    "DETAIL_ELEMENT_KEYS",
    "DETAIL_ELEMENT_LABELS",
    "ELEMENT_KEYS_PROMPT_BLOCK",
    "ELEMENT_KEY_ALIASES",
    "build_prompt_block",
    "is_allowed_key",
    "is_core_key",
    "is_detail_key",
    "label_for",
    "normalize_key",
]
