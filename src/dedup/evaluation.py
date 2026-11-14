"""评估纪律：调参集与留存集严格隔离。

人工标签分两类用途：
- tuning：只用于检查候选召回、阻断漏配原因和算法误合，允许据此修改政策；
- heldout：只用于一次性独立表现评估，禁止用于调阈值，也禁止出现在政策的 tuned_on_pairs 中。

本模块在代码层面强制执行隔离：
1. 两个集合的记录对必须不相交，重叠直接报错；
2. 留存集对若出现在政策 tuned_on_pairs 中，直接报错，防止“调完阈值再声称独立”；
3. 留存集评估只读取聚类/证据结果，不接受任何会写回政策的参数。
"""
from __future__ import annotations

from typing import Any, Mapping

from .blocking import blocking_diagnostics, candidate_pairs, pair_key
from .cluster import ClusteringResult
from .evidence import DECISION_AUTO_DIFFERENT, DECISION_AUTO_SAME, Policy


def _normalize_labels(raw: Mapping[str, Any]) -> dict[str, set[tuple[str, str]]]:
    # 兼容两种布局：独立标签文件（顶层即 tuning/heldout）
    # 与资料包内嵌（包在 "labels" 键下）。
    if "tuning" not in raw and "heldout" not in raw and isinstance(raw.get("labels"), Mapping):
        raw = raw["labels"]
    labels: dict[str, set[tuple[str, str]]] = {"tuning": set(), "heldout": set()}
    for split in ("tuning", "heldout"):
        entries = raw.get(split, [])
        if not isinstance(entries, list):
            raise ValueError(f"labels.{split} 必须为列表")
        for entry in entries:
            if not isinstance(entry, Mapping) or "pair" not in entry:
                raise ValueError(f"labels.{split} 的每项必须含 pair")
            pair = entry["pair"]
            if not (isinstance(pair, list) and len(pair) == 2
                    and all(isinstance(item, str) for item in pair)):
                raise ValueError(f"labels.{split} 的 pair 必须为两个 record_id")
            label = entry.get("label", "same")
            if label not in ("same", "different"):
                raise ValueError(f"labels.{split} 的 label 仅允许 same/different")
            if label == "same":
                labels[split].add(pair_key(*pair))
    overlap = labels["tuning"] & labels["heldout"]
    if overlap:
        raise ValueError("调参集与留存集存在重叠对，违反隔离纪律："
                         + ", ".join(f"{a}|{b}" for a, b in sorted(overlap)))
    return labels


def _same_cluster_pairs(clustering: ClusteringResult) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for members in clustering.clusters.values():
        ordered = sorted(members)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                result.add(pair_key(left, right))
    return result


def _labels_root(labels: Mapping[str, Any]) -> Mapping[str, Any]:
    if "tuning" not in labels and "heldout" not in labels and isinstance(labels.get("labels"), Mapping):
        return labels["labels"]
    return labels


def tuning_recall_audit(records: list, labels: Mapping[str, Any],
                        evidence: Mapping | None = None,
                        clustering: ClusteringResult | None = None) -> dict:
    """调参集专用：候选召回、阻断漏配原因与算法误合清单。

    误合分析需要 evidence/clustering；未传入时只做候选层审计。
    调参集允许据此修改政策，留存集评估走 :func:`evaluate_heldout` 且只读。
    """
    root = _labels_root(labels)
    split_labels = _normalize_labels(labels)
    tuning_pairs = split_labels["tuning"]
    diagnostics = blocking_diagnostics(records, tuning_pairs)
    candidates = candidate_pairs(records)
    covered = set(candidates.identity_pairs)
    audit = {
        "split": "tuning",
        "label_pairs": len(tuning_pairs),
        "candidate_recall": (len(tuning_pairs & covered) / len(tuning_pairs)) if tuning_pairs else None,
        "blocking": diagnostics,
        "note": "调参集结果可用于修改阻断键与政策；留存集不得用于此目的",
    }
    if evidence is not None and clustering is not None:
        different_pairs = {
            pair_key(*entry["pair"]) for entry in root.get("tuning", [])
            if entry.get("label", "same") == "different"
        }
        predicted_same = _same_cluster_pairs(clustering)
        auto_same = {pair for pair, item in evidence.items()
                     if item.decision == DECISION_AUTO_SAME}
        wrongly_merged = sorted(list(pair) for pair in (different_pairs & predicted_same))
        audit["merge_audit"] = {
            "label_different_pairs": len(different_pairs),
            "auto_same_pairs": len(auto_same),
            "wrongly_merged_pairs": wrongly_merged,
            "false_merge_rate": (len(wrongly_merged) / len(different_pairs))
            if different_pairs else None,
        }
    return audit


def evaluate_heldout(records: list, labels: Mapping[str, Any],
                     policy: Policy, evidence: Mapping,
                     clustering: ClusteringResult) -> dict:
    """留存集专用评估：只读，不回写政策；发现泄漏立即拒绝评估。"""
    split_labels = _normalize_labels(labels)
    labels_root = _labels_root(labels)
    heldout_pairs = split_labels["heldout"]
    tuned = {pair_key(*pair) for pair in policy.tuned_on_pairs}
    leakage = heldout_pairs & tuned
    if leakage:
        raise ValueError(
            "留存集对出现在政策 tuned_on_pairs 中，拒绝报告独立表现："
            + ", ".join(f"{a}|{b}" for a, b in sorted(leakage)))

    candidates = candidate_pairs(records)
    covered = set(candidates.identity_pairs)
    predicted_same = _same_cluster_pairs(clustering)

    missed = sorted(list(pair) for pair in (heldout_pairs - covered))
    # 留存集显式给出的 different 标签宇宙，误合只在该宇宙内核算
    different_pairs = {
        pair_key(*entry["pair"]) for entry in labels_root.get("heldout", [])
        if entry.get("label", "same") == "different"
    }
    wrongly_merged = sorted(list(pair) for pair in (different_pairs & predicted_same))
    auto_same = {pair for pair, item in evidence.items() if item.decision == DECISION_AUTO_SAME}
    auto_diff = {pair for pair, item in evidence.items() if item.decision == DECISION_AUTO_DIFFERENT}

    return {
        "split": "heldout",
        "label_same_pairs": len(heldout_pairs),
        "label_different_pairs": len(different_pairs),
        "candidate_recall": (len(heldout_pairs & covered) / len(heldout_pairs))
        if heldout_pairs else None,
        "candidate_misses": missed,
        "auto_same_correct": len(heldout_pairs & auto_same),
        "auto_same_on_labeled_pairs": len((auto_same | auto_diff) & (heldout_pairs | different_pairs)),
        "wrongly_merged_pairs": wrongly_merged,
        "false_merge_rate": (len(wrongly_merged) / len(different_pairs)) if different_pairs else None,
        "policy_config_hash": policy.config_hash(),
        "note": "本评估只读；任何阈值调整后必须更换留存集或重新走隔离评审",
    }
