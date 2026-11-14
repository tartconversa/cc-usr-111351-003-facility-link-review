"""候选生成：阻断键、漏配诊断与指定来源组合的全对核查。

阻断只负责“把值得比较的记录对送出来”，永远不产生同一/不同结论。
每个候选对都带有产出它的阻断规则名（provenance），便于解释与召回分析。
同类型（机构-机构、基地-基地）记录才进入实体同一候选；
跨类型只生成母子/归属（affiliation）候选，两者从不混入同一聚类。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable

from .model import Record
from .textkit import normalize_name

BLOCKER_NAME_PREFIX = "name_prefix"
BLOCKER_NAME_BIGRAM = "name_bigram"
BLOCKER_ADDRESS = "address_district"
BLOCKER_REGISTRATION = "registration_exact"
BLOCKER_AFFILIATION_REF = "affiliation_parent_ref"
BLOCKER_AFFILIATION_NAME = "affiliation_name_containment"
IDENTITY_BLOCKERS = (BLOCKER_NAME_PREFIX, BLOCKER_NAME_BIGRAM,
                     BLOCKER_ADDRESS, BLOCKER_REGISTRATION)
AFFILIATION_BLOCKERS = (BLOCKER_AFFILIATION_REF, BLOCKER_AFFILIATION_NAME)
PREFIX_LENGTH = 2


def pair_key(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))  # type: ignore[return-value]


def _add_pair(target: dict[tuple[str, str], list[str]], left: str, right: str, blocker: str) -> None:
    key = pair_key(left, right)
    target.setdefault(key, [])
    if blocker not in target[key]:
        target[key].append(blocker)


def _count_emitted(sources: dict[tuple[str, str], list[str]], blocker: str) -> int:
    return sum(1 for names in sources.values() if blocker in names)


@dataclass
class CandidateSet:
    identity_pairs: list[tuple[str, str]]
    relation_pairs: list[tuple[str, str]]
    pair_provenance: dict[tuple[str, str], list[str]]
    relation_provenance: dict[tuple[str, str], list[str]]
    blocker_stats: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "identity_pairs": [list(pair) for pair in self.identity_pairs],
            "relation_pairs": [list(pair) for pair in self.relation_pairs],
            "pair_provenance": {f"{a}|{b}": sources for (a, b), sources in self.pair_provenance.items()},
            "relation_provenance": {f"{a}|{b}": sources for (a, b), sources in self.relation_provenance.items()},
            "blocker_stats": self.blocker_stats,
        }


def candidate_pairs(records: list[Record]) -> CandidateSet:
    """按固定阻断规则集合生成候选对，输出可审计的生成来源与排除统计。"""
    identity_sources: dict[tuple[str, str], list[str]] = {}
    relation_sources: dict[tuple[str, str], list[str]] = {}
    blocker_stats: dict[str, dict] = {}

    # 同类型名称前缀
    prefix_buckets: dict[tuple, list[str]] = {}
    name_missing = name_short = 0
    for record in records:
        normalized = normalize_name(record.name)
        if normalized is None:
            name_missing += 1
            continue
        if len(normalized) < PREFIX_LENGTH:
            name_short += 1
            continue
        prefix_buckets.setdefault((record.kind, normalized[:PREFIX_LENGTH]), []).append(record.record_id)
    for members in prefix_buckets.values():
        for left, right in combinations(sorted(members), 2):
            _add_pair(identity_sources, left, right, BLOCKER_NAME_PREFIX)
    blocker_stats[BLOCKER_NAME_PREFIX] = {
        "buckets": len(prefix_buckets),
        "excluded_missing_name": name_missing,
        "excluded_short_name": name_short,
        "pairs_emitted": _count_emitted(identity_sources, BLOCKER_NAME_PREFIX),
    }

    # 同类型名称二元字组（中文短名的共享字组）
    bigram_buckets: dict[tuple, list[str]] = {}
    for record in records:
        normalized = normalize_name(record.name)
        if not normalized or len(normalized) < PREFIX_LENGTH:
            continue
        for index in range(len(normalized) - 1):
            bigram_buckets.setdefault((record.kind, normalized[index:index + 2]), []).append(record.record_id)
    for members in bigram_buckets.values():
        for left, right in combinations(sorted(set(members)), 2):
            _add_pair(identity_sources, left, right, BLOCKER_NAME_BIGRAM)
    blocker_stats[BLOCKER_NAME_BIGRAM] = {
        "buckets": len(bigram_buckets),
        "pairs_emitted": _count_emitted(identity_sources, BLOCKER_NAME_BIGRAM),
    }

    # 同类型省市区一致
    address_buckets: dict[tuple, list[str]] = {}
    for record in records:
        address = record.address
        if address is None or address.province is None or address.city is None or address.district is None:
            continue
        key = (record.kind, address.province, address.city, address.district)
        address_buckets.setdefault(key, []).append(record.record_id)
    for members in address_buckets.values():
        for left, right in combinations(sorted(set(members)), 2):
            _add_pair(identity_sources, left, right, BLOCKER_ADDRESS)
    complete = sum(1 for record in records if record.address is not None
                   and record.address.province is not None
                   and record.address.city is not None and record.address.district is not None)
    blocker_stats[BLOCKER_ADDRESS] = {
        "buckets": len(address_buckets),
        "excluded_incomplete_address": len(records) - complete,
        "pairs_emitted": _count_emitted(identity_sources, BLOCKER_ADDRESS),
    }

    # 同类型注册号精确一致（只生成候选，硬性规则在 evidence 层解释）
    registration_buckets: dict[tuple, list[str]] = {}
    for record in records:
        if record.registration is None:
            continue
        registration_buckets.setdefault((record.kind, record.registration), []).append(record.record_id)
    for members in registration_buckets.values():
        for left, right in combinations(sorted(set(members)), 2):
            _add_pair(identity_sources, left, right, BLOCKER_REGISTRATION)
    blocker_stats[BLOCKER_REGISTRATION] = {
        "buckets": len(registration_buckets),
        "excluded_missing_registration": sum(1 for record in records if record.registration is None),
        "pairs_emitted": _count_emitted(identity_sources, BLOCKER_REGISTRATION),
    }

    # 跨类型归属候选：显式 parent_id 引用（悬空引用计入统计，不静默生成）
    organizations = {record.record_id: record for record in records if record.kind == "organization"}
    facilities = [record for record in records if record.kind == "facility"]
    ref_missing = 0
    for facility in facilities:
        if facility.parent_id is None:
            continue
        if facility.parent_id not in organizations:
            ref_missing += 1
            continue
        _add_pair(relation_sources, facility.record_id, facility.parent_id, BLOCKER_AFFILIATION_REF)
    blocker_stats[BLOCKER_AFFILIATION_REF] = {
        "pairs_emitted": _count_emitted(relation_sources, BLOCKER_AFFILIATION_REF),
        "dangling_parent_ref": ref_missing,
    }

    # 跨类型归属候选：机构名包含于基地名，且至少一个共有地址片段
    for facility in facilities:
        facility_name = normalize_name(facility.name)
        if facility_name is None:
            continue
        for organization in organizations.values():
            org_name = normalize_name(organization.name)
            if not org_name or org_name not in facility_name:
                continue
            if _share_address_fragment(facility, organization):
                _add_pair(relation_sources, facility.record_id, organization.record_id,
                          BLOCKER_AFFILIATION_NAME)
    blocker_stats[BLOCKER_AFFILIATION_NAME] = {
        "pairs_emitted": _count_emitted(relation_sources, BLOCKER_AFFILIATION_NAME),
    }

    return CandidateSet(
        identity_pairs=sorted(identity_sources),
        relation_pairs=sorted(relation_sources),
        pair_provenance={key: list(sources) for key, sources in identity_sources.items()},
        relation_provenance={key: list(sources) for key, sources in relation_sources.items()},
        blocker_stats=blocker_stats,
    )


def _share_address_fragment(left: Record, right: Record) -> bool:
    if left.address is None or right.address is None:
        return False
    for field_name in ("province", "city", "district", "street"):
        value_a = getattr(left.address, field_name)
        value_b = getattr(right.address, field_name)
        if value_a is not None and value_a == value_b:
            return True
    return False


def all_pairs_for_source_combos(records: list[Record],
                                combos: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """对指定来源组合做小规模全对枚举（仅同类型实体对）。

    combos 中的来源对无序，如 ``("catalog", "expo")``；同来源写 ``("park", "park")``。
    用于人工核查阻断是否漏配，与候选生成结果对照。
    """
    wanted = {tuple(sorted(combo)) for combo in combos}
    result: list[tuple[str, str]] = []
    for left, right in combinations(records, 2):
        if left.kind != right.kind:
            continue
        if tuple(sorted((left.source, right.source))) in wanted:
            result.append(pair_key(left.record_id, right.record_id))
    return sorted(result)


def blocking_diagnostics(records: list[Record],
                         true_pairs: Iterable[tuple[str, str]]) -> dict:
    """报告阻断规则对已知同实体对（调参标签）的漏配情况及可能原因。

    输出每个阻断规则的命中/漏配计数，并对每个漏配对给出逐条规则的漏配原因，
    使“哪些阻断规则可能漏配”有数据依据，而不是凭感觉调键。
    """
    by_id = {record.record_id: record for record in records}
    candidates = candidate_pairs(records)
    covered = set(candidates.identity_pairs)
    true_set = {pair_key(*pair) for pair in true_pairs}
    missing = sorted(true_set - covered)

    per_blocker: dict[str, dict] = {}
    for blocker in IDENTITY_BLOCKERS:
        hit = sum(1 for pair in true_set if blocker in candidates.pair_provenance.get(pair, []))
        per_blocker[blocker] = {"hits": hit, "misses": len(true_set) - hit}

    reasons: list[dict] = []
    for pair in missing:
        record_a, record_b = by_id[pair[0]], by_id[pair[1]]
        pair_reasons = {
            BLOCKER_NAME_PREFIX: _prefix_miss_reason(record_a, record_b),
            BLOCKER_NAME_BIGRAM: _bigram_miss_reason(record_a, record_b),
            BLOCKER_ADDRESS: _address_miss_reason(record_a, record_b),
            BLOCKER_REGISTRATION: _registration_miss_reason(record_a, record_b),
        }
        reasons.append({"pair": list(pair), "reasons": pair_reasons})

    reason_counts: dict[str, dict[str, int]] = {blocker: {} for blocker in IDENTITY_BLOCKERS}
    for item in reasons:
        for blocker, reason in item["reasons"].items():
            reason_counts[blocker][reason] = reason_counts[blocker].get(reason, 0) + 1
    return {
        "true_pairs": len(true_set),
        "covered_pairs": len(true_set & covered),
        "missing_pairs": len(missing),
        "per_blocker": per_blocker,
        "miss_reason_counts": reason_counts,
        "missing_detail": reasons,
    }


def _prefix_miss_reason(left: Record, right: Record) -> str:
    names = [normalize_name(left.name), normalize_name(right.name)]
    if any(name is None for name in names):
        return "name_missing"
    if any(len(name) < PREFIX_LENGTH for name in names):
        return "name_too_short"
    if names[0][:PREFIX_LENGTH] != names[1][:PREFIX_LENGTH]:
        return "prefix_differs"
    return "kind_differs" if left.kind != right.kind else "covered"


def _bigram_miss_reason(left: Record, right: Record) -> str:
    names = [normalize_name(left.name), normalize_name(right.name)]
    if any(not name or len(name) < PREFIX_LENGTH for name in names):
        return "name_missing_or_short"
    shared = {names[0][i:i + 2] for i in range(len(names[0]) - 1)} & \
        {names[1][i:i + 2] for i in range(len(names[1]) - 1)}
    if not shared:
        return "no_shared_bigram"
    return "kind_differs" if left.kind != right.kind else "covered"


def _address_miss_reason(left: Record, right: Record) -> str:
    if left.address is None or right.address is None:
        return "address_missing"
    fragments_a = (left.address.province, left.address.city, left.address.district)
    fragments_b = (right.address.province, right.address.city, right.address.district)
    if any(fragment is None for fragment in fragments_a + fragments_b):
        return "address_incomplete"
    if fragments_a != fragments_b:
        return "district_key_differs"
    return "kind_differs" if left.kind != right.kind else "covered"


def _registration_miss_reason(left: Record, right: Record) -> str:
    if left.registration is None or right.registration is None:
        return "registration_missing"
    if left.registration != right.registration:
        return "registration_differs"
    return "kind_differs" if left.kind != right.kind else "covered"
