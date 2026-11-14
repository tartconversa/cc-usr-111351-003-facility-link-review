"""名称与地址的软证据计算。

设计约束：
- 名称规范化（去空格、全半角统一、大小写折叠）只用于相似度与阻断键，
  规范化后相等本身永远不是硬性合并依据；
- 缺失字段（``None``）不参与任何相等判断，相似度返回 ``None`` 而不是 0 或 1；
- 同音/近形字表是政策数据，可配置、可审计，默认表保守且小；
- 仅使用标准库（``unicodedata`` 与 ``difflib``），字符级相似度可解释、可复现。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable

from .model import Address, ADDRESS_FIELDS

# 同音/近形等价组：同组字符在“同音感知”比较中视为可互换。
# 这是弱证据来源，默认表刻意保持小而明确，新增等价组必须在政策版本中留痕。
DEFAULT_HOMOPHONE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("材", "才"),
    ("青", "清"),
    ("一", "1", "①"),
    ("二", "2", "②"),
    ("园", "園", "元"),
    ("厂", "廠", "场"),
)

# 名称中出现这些层级/关系字样时，提示“母子或集团/基地混淆”风险，
# 仅作为风险标记输出，不直接产生同一或不同判定。
RELATION_MARKERS = ("集团", "总公司", "母公司", "分公司", "基地", "分厂", "一厂", "二厂", "三厂")

_PUNCTUATION = "()（）[]【】、·.,，。-—_/\\ \t　"


def _lookup(group_table: Iterable[Iterable[str]]) -> dict[str, str]:
    table: dict[str, str] = {}
    for group in group_table:
        canonical = group[0]
        for char in group:
            if char in table and table[char] != canonical:
                raise ValueError("同音等价组存在重叠字符：" + char)
            table[char] = canonical
    return table


def normalize_name(name: str | None) -> str | None:
    """NFKC 归一化、大小写折叠并去除空白与常见标点。

    结果用于比较，不回写记录；``None`` 透传。
    """
    if name is None:
        return None
    folded = unicodedata.normalize("NFKC", name).casefold()
    return "".join(char for char in folded if char not in _PUNCTUATION)


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


@dataclass(frozen=True)
class NameFeatures:
    """一对名称的可解释特征，缺失名称时全部比率为 None。"""

    both_present: bool
    exact_raw_equal: bool | None
    normalized_equal: bool | None
    char_ratio: float | None
    homophone_ratio: float | None
    relation_markers: tuple[str, ...]
    homophone_hits: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "both_present": self.both_present,
            "exact_raw_equal": self.exact_raw_equal,
            "normalized_equal": self.normalized_equal,
            "char_ratio": None if self.char_ratio is None else round(self.char_ratio, 4),
            "homophone_ratio": None if self.homophone_ratio is None else round(self.homophone_ratio, 4),
            "relation_markers": list(self.relation_markers),
            "homophone_hits": list(self.homophone_hits),
        }


def name_features(name_a: str | None, name_b: str | None,
                  homophone_groups: Iterable[Iterable[str]] = DEFAULT_HOMOPHONE_GROUPS) -> NameFeatures:
    if name_a is None or name_b is None:
        return NameFeatures(False, None, None, None, None, ())
    normalized_a = normalize_name(name_a)
    normalized_b = normalize_name(name_b)
    table = _lookup(homophone_groups)

    def fold_homophones(text: str) -> str:
        return "".join(table.get(char, char) for char in text)

    folded_a = fold_homophones(normalized_a)
    folded_b = fold_homophones(normalized_b)
    # 只报告“被替换掉”的非规范字（映射后不等于自身的字符），规范字不算命中
    hits = tuple(sorted({
        char for char in normalized_a + normalized_b
        if char in table and char != table[char]
    }))
    markers = tuple(marker for marker in RELATION_MARKERS if marker in name_a or marker in name_b)
    return NameFeatures(
        both_present=True,
        exact_raw_equal=name_a == name_b,
        normalized_equal=normalized_a == normalized_b,
        char_ratio=_ratio(normalized_a, normalized_b),
        homophone_ratio=_ratio(folded_a, folded_b),
        relation_markers=markers,
        homophone_hits=hits,
    )


# 地址离散判定等级，语义见 README；冲突优先于一切“部分匹配”。
ADDR_LEVELS = ("identical", "compatible", "weak", "conflict", "unknown")


@dataclass(frozen=True)
class FragmentStatus:
    field: str
    value_a: str | None
    value_b: str | None
    status: str  # equal / differ / only_a / only_b / missing

    def as_dict(self) -> dict:
        return {"field": self.field, "value_a": self.value_a,
                "value_b": self.value_b, "status": self.status}


@dataclass(frozen=True)
class AddressFeatures:
    level: str
    compared_fragments: int
    matched_fragments: int
    differing_fragments: int
    coverage: float | None
    fragments: tuple[FragmentStatus, ...]

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "compared_fragments": self.compared_fragments,
            "matched_fragments": self.matched_fragments,
            "differing_fragments": self.differing_fragments,
            "coverage": None if self.coverage is None else round(self.coverage, 4),
            "fragments": [item.as_dict() for item in self.fragments],
        }


def compare_addresses(address_a: Address | None, address_b: Address | None) -> AddressFeatures:
    """逐片段比较地址。双方都缺失的片段不产生任何证据。

    coverage = 双方都有值的片段数 / 信息更全一方的非空片段数；
    信息整体缺失时 coverage 为 None，等级为 unknown。
    """
    fragments: list[FragmentStatus] = []
    compared = matched = differing = 0
    present_sides = [0, 0]
    for name in ADDRESS_FIELDS:
        value_a = getattr(address_a, name) if address_a is not None else None
        value_b = getattr(address_b, name) if address_b is not None else None
        if value_a is not None:
            present_sides[0] += 1
        if value_b is not None:
            present_sides[1] += 1
        if value_a is None and value_b is None:
            status = "missing"
        elif value_a is None:
            status = "only_b"
        elif value_b is None:
            status = "only_a"
        elif value_a == value_b:
            status = "equal"
            compared += 1
            matched += 1
        else:
            status = "differ"
            compared += 1
            differing += 1
        fragments.append(FragmentStatus(name, value_a, value_b, status))

    richest = max(present_sides)
    coverage = (compared / richest) if richest > 0 else None

    if richest == 0:
        level = "unknown"
    elif differing:
        level = "conflict"
    elif compared == 0:
        level = "unknown" if richest else "unknown"
    elif matched >= 3 or any(item.field == "detail" and item.status == "equal" for item in fragments):
        level = "identical"
    elif matched >= 1:
        level = "compatible"
    else:
        level = "weak"
    return AddressFeatures(level, compared, matched, differing, coverage, tuple(fragments))
