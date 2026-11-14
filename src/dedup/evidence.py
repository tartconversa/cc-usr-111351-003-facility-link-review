"""成对证据：硬性规则、软证据与可解释的离散判定。

关键原则：
- 硬性规则（层级不同、同时有效且注册号不同）直接决定不可合并，任何相似度都不能推翻；
- 名称、地址相似度只是软证据，缺失字段不产生相等证据；
- 软分数仅用于待审队列排序，并以离散等级（strong/weak/none）和明确的规则编号解释；
- 自动判定（auto_same / auto_different）走的是显式规则，不是“分数超过某个阈值”；
- 人工审阅是另一条事实线，在 reviews 模块处理，本模块不读取也不输出人工结论。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .blocking import pair_key
from .model import Record, evidence_version, intervals_overlap
from .textkit import (DEFAULT_HOMOPHONE_GROUPS, AddressFeatures, NameFeatures,
                      compare_addresses, name_features)

# 软证据等级
LEVEL_STRONG = "strong"
LEVEL_WEAK = "weak"
LEVEL_NONE = "none"

# 判定结果
DECISION_AUTO_SAME = "auto_same"
DECISION_AUTO_DIFFERENT = "auto_different"
DECISION_REVIEW = "review"
DECISION_RELATION = "relation"

# 规则编号（写入每条判定的 reasons，审阅界面可直接引用）
RULE_KIND_MISMATCH = "H0-kind-mismatch"
RULE_REG_CONFLICT = "H1-registration-conflict-while-overlapping"
RULE_REG_MATCH = "S0-registration-equal"
RULE_NAME_STRONG = "S1-name-strong"
RULE_NAME_WEAK = "S2-name-weak"
RULE_NAME_NONE = "S3-name-no-evidence"
RULE_ADDR_IDENTICAL = "S4-address-identical"
RULE_ADDR_COMPATIBLE = "S5-address-compatible"
RULE_ADDR_CONFLICT = "S6-address-conflict"
RULE_ADDR_UNKNOWN = "S7-address-unknown"
RULE_DISJOINT_PERIOD = "S8-disjoint-validity-periods"
RULE_AUTO_SAME = "A1-auto-same-policy"
RULE_AUTO_DIFF = "A2-auto-different-policy"
RULE_RELATION_CANDIDATE = "R1-cross-kind-relation-candidate"


@dataclass(frozen=True)
class Policy:
    """可配置、可版本化的判定政策。权重只影响队列排序分，不改变硬规则。"""

    version: str = "default-1"
    weight_name_char: float = 0.45
    weight_name_homophone: float = 0.15
    weight_address: float = 0.40
    name_strong_ratio: float = 0.80
    name_weak_ratio: float = 0.55
    auto_same_name_levels: tuple[str, ...] = (LEVEL_STRONG,)
    auto_same_address_levels: tuple[str, ...] = ("identical", "compatible")
    auto_different_name_levels: tuple[str, ...] = (LEVEL_NONE,)
    address_conflict_blocks_auto_same: bool = True
    homophone_groups: tuple[tuple[str, ...], ...] = DEFAULT_HOMOPHONE_GROUPS
    # 调参所用标签对记录在此；留存集对不得出现，由 evaluation 模块强制校验。
    tuned_on_pairs: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_config(cls, config: Mapping[str, Any] | None) -> "Policy":
        if config is None:
            return cls()
        allowed = {field_name for field_name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(config) - allowed
        if unknown:
            raise ValueError("政策配置含未知字段：" + ",".join(sorted(unknown)))
        kwargs = dict(config)
        if "auto_same_name_levels" in kwargs:
            kwargs["auto_same_name_levels"] = tuple(kwargs["auto_same_name_levels"])
        if "auto_same_address_levels" in kwargs:
            kwargs["auto_same_address_levels"] = tuple(kwargs["auto_same_address_levels"])
        if "homophone_groups" in kwargs:
            kwargs["homophone_groups"] = tuple(tuple(group) for group in kwargs["homophone_groups"])
        if "tuned_on_pairs" in kwargs:
            kwargs["tuned_on_pairs"] = tuple(tuple(sorted(pair)) for pair in kwargs["tuned_on_pairs"])
        return cls(**kwargs)

    def config_hash(self) -> str:
        payload = {key: getattr(self, key) for key in self.__dataclass_fields__}  # type: ignore[attr-defined]
        from .model import canonical_json
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "config_hash": self.config_hash(),
            "weight_name_char": self.weight_name_char,
            "weight_name_homophone": self.weight_name_homophone,
            "weight_address": self.weight_address,
            "name_strong_ratio": self.name_strong_ratio,
            "name_weak_ratio": self.name_weak_ratio,
            "auto_same_name_levels": list(self.auto_same_name_levels),
            "auto_same_address_levels": list(self.auto_same_address_levels),
            "auto_different_name_levels": list(self.auto_different_name_levels),
            "address_conflict_blocks_auto_same": self.address_conflict_blocks_auto_same,
            "homophone_groups": [list(group) for group in self.homophone_groups],
            "tuned_on_pairs": [list(pair) for pair in self.tuned_on_pairs],
        }


@dataclass(frozen=True)
class PairEvidence:
    pair: tuple[str, str]
    kind_a: str
    kind_b: str
    source_a: str
    source_b: str
    evidence_version: str
    overlap: bool
    hard_different: bool
    hard_reasons: tuple[str, ...]
    name: dict
    name_level: str | None
    address: dict
    address_level: str | None
    soft_score: float | None
    decision: str
    decision_basis: tuple[str, ...]
    relation_proposal: bool = False

    def as_dict(self) -> dict:
        return {
            "pair": list(self.pair),
            "kinds": [self.kind_a, self.kind_b],
            "sources": [self.source_a, self.source_b],
            "evidence_version": self.evidence_version,
            "validity_overlap": self.overlap,
            "hard_different": self.hard_different,
            "hard_reasons": list(self.hard_reasons),
            "name": self.name,
            "name_level": self.name_level,
            "address": self.address,
            "address_level": self.address_level,
            "soft_score": None if self.soft_score is None else round(self.soft_score, 4),
            "decision": self.decision,
            "decision_basis": list(self.decision_basis),
            "relation_proposal": self.relation_proposal,
        }


def _name_level(features: NameFeatures, policy: Policy) -> tuple[str | None, list[str]]:
    if not features.both_present:
        return None, [RULE_NAME_NONE]
    basis: list[str] = []
    if features.normalized_equal:
        basis.append(RULE_NAME_STRONG)
        return LEVEL_STRONG, basis
    level = LEVEL_NONE
    ratio = features.char_ratio or 0.0
    homophone_ratio = features.homophone_ratio or 0.0
    best = max(ratio, homophone_ratio)
    if best >= policy.name_strong_ratio:
        level = LEVEL_STRONG
        basis.append(RULE_NAME_STRONG)
    elif best >= policy.name_weak_ratio:
        level = LEVEL_WEAK
        basis.append(RULE_NAME_WEAK)
    else:
        basis.append(RULE_NAME_NONE)
    if features.homophone_hits:
        # 同音命中只解释“为什么像”，不单独抬升等级。
        basis.append("S2a-homophone-hits-explained")
    if features.relation_markers:
        basis.append("S2b-relation-marker-risk:" + ",".join(features.relation_markers))
    return level, basis


def _address_level_rule(level: str | None) -> str:
    return {
        "identical": RULE_ADDR_IDENTICAL,
        "compatible": RULE_ADDR_COMPATIBLE,
        "conflict": RULE_ADDR_CONFLICT,
        "weak": RULE_ADDR_COMPATIBLE,
        "unknown": RULE_ADDR_UNKNOWN,
    }.get(level or "unknown", RULE_ADDR_UNKNOWN)


def _soft_score(name: NameFeatures, address: AddressFeatures, policy: Policy) -> float | None:
    """队列排序分：各部分缺失时按“仅在有证据的维度上归一化”计算。

    缺失不补零也不补一：若所有维度都缺失，返回 None（该对不参与分数排序）。
    """
    parts: list[tuple[float, float]] = []
    if name.both_present:
        best_name = max(name.char_ratio or 0.0, name.homophone_ratio or 0.0)
        parts.append((best_name, policy.weight_name_char + policy.weight_name_homophone))
    if address.level != "unknown":
        addr_score = 0.0
        if address.level == "identical":
            addr_score = 1.0
        elif address.level == "compatible":
            addr_score = 0.6
        elif address.level == "conflict":
            addr_score = 0.0
        elif address.level == "weak":
            addr_score = 0.3
        parts.append((addr_score, policy.weight_address))
    if not parts:
        return None
    total_weight = sum(weight for _, weight in parts)
    return sum(value * weight for value, weight in parts) / total_weight


def build_pair_evidence(record_a: Record, record_b: Record, policy: Policy | None = None,
                        relation_candidate: bool = False) -> PairEvidence:
    """为一对记录构建完整证据与离散判定。跨类型对只产出归属提案。"""
    policy = policy or Policy()
    pair = pair_key(record_a.record_id, record_b.record_id)
    # 规范化顺序，使证据只依赖记录内容，不依赖传入顺序
    if (record_a.record_id, record_b.record_id) != pair:
        record_a, record_b = record_b, record_a

    overlap = intervals_overlap(record_a, record_b)
    hard_reasons: list[str] = []
    if record_a.kind != record_b.kind:
        hard_reasons.append(RULE_KIND_MISMATCH)

    conflict_reg = (
        record_a.registration is not None
        and record_b.registration is not None
        and record_a.registration != record_b.registration
    )
    if conflict_reg and overlap:
        hard_reasons.append(RULE_REG_CONFLICT)
    hard_different = bool(hard_reasons)

    name = name_features(record_a.name, record_b.name, policy.homophone_groups)
    address = compare_addresses(record_a.address, record_b.address)
    name_level, name_basis = _name_level(name, policy)
    addr_basis = [_address_level_rule(address.level)]
    if not overlap:
        addr_basis.append(RULE_DISJOINT_PERIOD)
    if conflict_reg and not overlap:
        # 不重叠的注册号差异不是硬冲突，但必须显式提示，禁止静默合并
        addr_basis.append("S9-registration-diff-in-disjoint-periods")

    soft_score = _soft_score(name, address, policy)
    basis = name_basis + addr_basis

    if record_a.kind != record_b.kind:
        decision = DECISION_RELATION
        basis.append(RULE_RELATION_CANDIDATE if relation_candidate else "R2-cross-kind-no-relation-candidate")
        return PairEvidence(
            pair=pair, kind_a=record_a.kind, kind_b=record_b.kind,
            source_a=record_a.source, source_b=record_b.source,
            evidence_version=evidence_version(record_a, record_b),
            overlap=overlap, hard_different=True, hard_reasons=tuple(hard_reasons),
            name=name.as_dict(), name_level=None,
            address=address.as_dict(), address_level=None,
            soft_score=soft_score, decision=decision, decision_basis=tuple(basis),
            relation_proposal=relation_candidate,
        )

    if hard_different:
        decision = DECISION_AUTO_DIFFERENT
        basis.insert(0, RULE_AUTO_DIFF)
        return PairEvidence(
            pair=pair, kind_a=record_a.kind, kind_b=record_b.kind,
            source_a=record_a.source, source_b=record_b.source,
            evidence_version=evidence_version(record_a, record_b),
            overlap=overlap, hard_different=True, hard_reasons=tuple(hard_reasons),
            name=name.as_dict(), name_level=name_level,
            address=address.as_dict(), address_level=address.level,
            soft_score=soft_score, decision=decision, decision_basis=tuple(basis),
        )

    same_reg = (
        record_a.registration is not None
        and record_b.registration is not None
        and record_a.registration == record_b.registration
    )
    if same_reg:
        basis.append(RULE_REG_MATCH)

    name_ok = name_level in policy.auto_same_name_levels
    addr_ok = address.level in policy.auto_same_address_levels
    addr_blocked = policy.address_conflict_blocks_auto_same and address.level == "conflict"
    name_weak_ok = name_level == LEVEL_WEAK
    # 自动同一：强名称 + 支持性地址 + 时段相交；注册号相等时名称可放宽到 weak（仍要求时段相交）。
    if overlap and not addr_blocked and (
        (name_ok and addr_ok) or (same_reg and (name_ok or name_weak_ok) and addr_ok)
    ):
        decision = DECISION_AUTO_SAME
        basis.insert(0, RULE_AUTO_SAME)
    elif name_level in policy.auto_different_name_levels and address.level != "identical":
        decision = DECISION_AUTO_DIFFERENT
        basis.insert(0, RULE_AUTO_DIFF)
    else:
        decision = DECISION_REVIEW
    return PairEvidence(
        pair=pair, kind_a=record_a.kind, kind_b=record_b.kind,
        source_a=record_a.source, source_b=record_b.source,
        evidence_version=evidence_version(record_a, record_b),
        overlap=overlap, hard_different=False, hard_reasons=(),
        name=name.as_dict(), name_level=name_level,
        address=address.as_dict(), address_level=address.level,
        soft_score=soft_score, decision=decision, decision_basis=tuple(basis),
    )


def build_all_evidence(records: list[Record], candidates: Iterable[Any],
                       policy: Policy, relation: bool = False) -> dict[tuple[str, str], PairEvidence]:
    """批量构建证据。candidates 为 (id_a, id_b) 可迭代对象。"""
    by_id = {record.record_id: record for record in records}
    result: dict[tuple[str, str], PairEvidence] = {}
    for pair in candidates:
        key = pair_key(*pair)
        result[key] = build_pair_evidence(by_id[key[0]], by_id[key[1]], policy,
                                          relation_candidate=relation)
    return result
