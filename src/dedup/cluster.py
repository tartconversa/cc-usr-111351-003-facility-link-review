"""全局一致的受约束聚类与冲突链处理。

候选层输出的是成对证据，本模块把它们拼成全局决策：
- 同一边只在证据为 auto_same，或人工确认同一（含已验证的人工边）时并入；
- 任何并入都必须先检查目标分量之间是否存在“硬性不同/人工确认不同”边；
  存在则拒绝并入，两个分量标记为冻结，并输出完整冲突链；
- 冻结分量停止发布（不产生簇标识、不进统计），但其中成员与证据照常保留；
- 簇标识由成员集合的内容哈希派生，与遍历顺序无关，撤回审阅后无关分量标识不变。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from .evidence import (DECISION_AUTO_DIFFERENT, DECISION_AUTO_SAME, PairEvidence)
from .model import canonical_json

# 人工边判定标签（与 reviews 模块的断言词汇一致）
HUMAN_SAME = "human_same"
HUMAN_DIFFERENT = "human_different"


class _UnionFind:
    def __init__(self, ids: list[str]):
        # 固定初始顺序，find 路径压缩不影响输出：最终以排序后的成员集生成标识
        self.parent = {identifier: identifier for identifier in sorted(ids)}

    def find(self, identifier: str) -> str:
        root = identifier
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[identifier] != root:
            self.parent[identifier], identifier = root, self.parent[identifier]
        return root

    def union(self, left: str, right: str) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            # 字典序较小者作根，仅为确定性，不参与标识
            if root_left < root_right:
                self.parent[root_right] = root_left
            else:
                self.parent[root_left] = root_right

    def members(self, root: str) -> list[str]:
        return sorted(identifier for identifier in self.parent if self.find(identifier) == root)


@dataclass
class Edge:
    pair: tuple[str, str]
    kind: str          # auto_same / human_same / auto_different / human_different
    origin: str        # algorithm / review:<review_id>
    basis: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"pair": list(self.pair), "kind": self.kind, "origin": self.origin,
                "basis": list(self.basis)}


@dataclass
class ConflictChain:
    chain: list[str]
    blocking_edge: Edge
    connecting_edge: Edge

    def as_dict(self) -> dict:
        return {
            "chain": list(self.chain),
            "blocking_edge": self.blocking_edge.as_dict(),
            "connecting_edge": self.connecting_edge.as_dict(),
        }


@dataclass
class ClusteringResult:
    clusters: dict[str, list[str]]
    cluster_ids_by_record: dict[str, str]
    frozen_members: list[str]
    conflict_chains: list[ConflictChain]
    applied_same_edges: list[Edge]
    blocked_same_edges: list[Edge]
    different_edges: list[Edge]
    singletons: list[str]

    def as_dict(self) -> dict:
        return {
            "clusters": {key: list(value) for key, value in sorted(self.clusters.items())},
            "cluster_ids_by_record": dict(sorted(self.cluster_ids_by_record.items())),
            "frozen_members": list(self.frozen_members),
            "conflict_chains": [item.as_dict() for item in self.conflict_chains],
            "applied_same_edges": [item.as_dict() for item in self.applied_same_edges],
            "blocked_same_edges": [item.as_dict() for item in self.blocked_same_edges],
            "different_edges": [item.as_dict() for item in self.different_edges],
            "singletons": list(self.singletons),
        }


def _cluster_identifier(members: list[str]) -> str:
    digest = hashlib.sha256(canonical_json(members).encode("utf-8")).hexdigest()[:10]
    return "ent_" + digest


def _edge_index(edges: list[Edge]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.pair[0], set()).add(edge.pair[1])
        adjacency.setdefault(edge.pair[1], set()).add(edge.pair[0])
    return adjacency


def _find_chain(adjacency: Mapping[str, set[str]], left: str, right: str) -> list[str]:
    """在“已并入同一分量”的边构成的图上，找 left→right 的具体路径，作为冲突链。"""
    queue = [[left]]
    seen = {left}
    while queue:
        path = queue.pop(0)
        node = path[-1]
        if node == right:
            return path
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(path + [neighbor])
    return [left, right]


def cluster_records(record_ids: list[str],
                    evidence: Mapping[tuple[str, str], PairEvidence],
                    human_edges: Mapping[tuple[str, str], Edge] | None = None) -> ClusteringResult:
    """把成对证据与人工边合并为全局聚类。

    处理顺序固定为：先算法 auto_same 边（按 pair 排序），再人工同一/不同边
    （按 pair 与 review 来源排序）。人工边先做硬约束校验，绝不被算法覆盖。
    """
    human_edges = human_edges or {}
    uf = _UnionFind(record_ids)

    same_edges = sorted(
        [Edge(pair=pair, kind=DECISION_AUTO_SAME, origin="algorithm",
              basis=item.decision_basis)
         for pair, item in evidence.items() if item.decision == DECISION_AUTO_SAME],
        key=lambda edge: edge.pair,
    )
    different_edges = sorted(
        [Edge(pair=pair, kind=DECISION_AUTO_DIFFERENT, origin="algorithm",
              basis=item.decision_basis)
         for pair, item in evidence.items() if item.decision == DECISION_AUTO_DIFFERENT],
        key=lambda edge: edge.pair,
    )
    for pair, edge in sorted(human_edges.items()):
        if edge.kind == HUMAN_SAME:
            same_edges.append(edge)
        elif edge.kind == HUMAN_DIFFERENT:
            different_edges.append(edge)
    same_edges.sort(key=lambda edge: (edge.pair, edge.origin))
    different_edges.sort(key=lambda edge: (edge.pair, edge.origin))

    applied: list[Edge] = []
    blocked: list[Edge] = []
    conflicts: list[ConflictChain] = []
    frozen: set[str] = set()
    # 诉求图：纳入“所有”同一诉求边（算法与人工、无论是否被阻断）。
    # 冲突链要表达的是“这些记录被同一诉求串起来、却撞上硬不同”，
    # 因此被阻断的边也必须在图中，否则会丢失传递链的首跳。
    claim_adjacency: dict[str, set[str]] = {}
    for edge in same_edges:
        claim_adjacency.setdefault(edge.pair[0], set()).add(edge.pair[1])
        claim_adjacency.setdefault(edge.pair[1], set()).add(edge.pair[0])

    def cross_component_diff(root_a: str, root_b: str) -> Edge | None:
        members_a = set(uf.members(root_a))
        members_b = set(uf.members(root_b))
        matches = [
            edge for edge in different_edges
            if (edge.pair[0] in members_a and edge.pair[1] in members_b)
            or (edge.pair[1] in members_a and edge.pair[0] in members_b)
        ]
        if not matches:
            return None
        # 人工确认不同优先于算法 auto_different 展示：人工意见绝不被算法边掩盖
        human = [edge for edge in matches if edge.origin != "algorithm"]
        return sorted(human or matches, key=lambda edge: (edge.pair, edge.origin))[0]

    for edge in same_edges:
        left, right = edge.pair
        root_left, root_right = uf.find(left), uf.find(right)
        if root_left == root_right:
            if edge not in applied:
                applied.append(edge)
            continue
        blocker = cross_component_diff(root_left, root_right)
        if blocker is not None or left in frozen or right in frozen:
            if blocker is not None:
                blocking = blocker
            else:
                blocking = next(
                    (item for item in different_edges
                     if item.pair[0] in (left, right) or item.pair[1] in (left, right)),
                    Edge(pair=edge.pair, kind=HUMAN_DIFFERENT, origin="frozen-component", basis=()),
                )
            # 冲突链连接的是“硬性不同边”的两个端点，经同一诉求图上的传递路径。
            # 用阻断边端点而非当前处理边端点，才能呈现完整链（如 B1→B2→B3）。
            full_chain = _find_chain(claim_adjacency, blocking.pair[0], blocking.pair[1])
            conflicts.append(ConflictChain(chain=full_chain, blocking_edge=blocking,
                                           connecting_edge=edge))
            blocked.append(edge)
            frozen.update(uf.members(root_left))
            frozen.update(uf.members(root_right))
            continue
        uf.union(left, right)
        applied.append(edge)

    # 任何含冲突的分量整体冻结（包括冲突链后续并进来的成员）
    for conflict in conflicts:
        for node in conflict.chain:
            frozen.update(uf.members(uf.find(node)))

    clusters: dict[str, list[str]] = {}
    ids_by_record: dict[str, str] = {}
    singletons: list[str] = []
    roots = {uf.find(identifier) for identifier in record_ids}
    for root in sorted(roots):
        members = uf.members(root)
        if any(member in frozen for member in members):
            continue
        if len(members) == 1:
            singletons.append(members[0])
        identifier = _cluster_identifier(members)
        clusters[identifier] = members
        for member in members:
            ids_by_record[member] = identifier

    return ClusteringResult(
        clusters=clusters,
        cluster_ids_by_record=ids_by_record,
        frozen_members=sorted(frozen),
        conflict_chains=conflicts,
        applied_same_edges=applied,
        blocked_same_edges=blocked,
        different_edges=different_edges,
        singletons=sorted(singletons),
    )


def diff_clusters(before: Mapping[str, list[str]],
                  after: Mapping[str, list[str]]) -> dict:
    """旧簇 → 新簇的成员变化。簇标识由成员集合派生，可直接做集合比对。"""
    before_sets = {key: set(value) for key, value in before.items()}
    after_sets = {key: set(value) for key, value in after.items()}
    old_to_new: dict[str, list[str]] = {}
    for old_id, old_members in before_sets.items():
        targets = sorted(
            new_id for new_id, new_members in after_sets.items()
            if old_members & new_members
        )
        old_to_new[old_id] = targets

    changed: list[dict] = []
    for old_id, targets in old_to_new.items():
        old_members = before_sets[old_id]
        new_union = set().union(*(after_sets[new_id] for new_id in targets)) if targets else set()
        if targets == [old_id] and old_members == new_union:
            continue
        changed.append({
            "old_cluster": old_id,
            "old_members": sorted(old_members),
            "new_clusters": targets,
            "new_members": sorted(new_union),
            "removed_from_old": sorted(old_members - new_union),
            "added_to_new": sorted(new_union - old_members),
        })
    new_only = sorted(
        new_id for new_id, new_members in after_sets.items()
        if not any(new_members & old_members for old_members in before_sets.values())
    )
    return {"changed_clusters": changed, "new_only_clusters": new_only,
            "stable": not changed and not new_only}
