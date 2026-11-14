"""可复现发布包组装。

发布包是后续技术覆盖分析的唯一输入，其内容完全由资料指纹、政策配置、
审阅账本轨迹与确定性算法决定，不含时间戳、环境路径或随机数。
冻结分量（含未解决的人工冲突链）不发布、不进统计；
待审对不阻断发布，但单独成队列并标注其不参与统计。
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .blocking import CandidateSet
from .cluster import ClusteringResult
from .evidence import PairEvidence, Policy
from .model import Record, canonical_json, record_fingerprint
from .reviews import ASSERT_DIFFERENT, ASSERT_SAME


def _publication_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _review_queue(evidence: Mapping[tuple[str, str], PairEvidence]) -> list[dict]:
    queue = []
    for pair, item in sorted(evidence.items()):
        if item.decision != "review":
            continue
        queue.append({
            "pair": list(pair),
            "soft_score": item.soft_score,
            "name_level": item.name_level,
            "address_level": item.address_level,
            "validity_overlap": item.overlap,
            "reasons": list(item.decision_basis),
        })
    # 分数仅用于队列内排序；None 排最后，顺序本身不产生任何事实
    queue.sort(key=lambda item: (item["soft_score"] is None,
                                 -(item["soft_score"] or 0.0), item["pair"]))
    return queue


def assemble_publication(records: list[Record],
                         policy: Policy,
                         candidates: CandidateSet,
                         evidence: Mapping[tuple[str, str], PairEvidence],
                         clustering: ClusteringResult,
                         ledger_result: Mapping[str, Any],
                         data_fingerprint: str) -> dict:
    """组装一次完整发布。返回 JSON 可序列化的字典，附带整体复现哈希。"""
    frozen_set = set(clustering.frozen_members)
    published_members = {
        member for members in clustering.clusters.values() for member in members
    }

    relation_edges: list[dict] = []
    for pair, proposal in sorted(candidates.relation_provenance.items()):
        item = evidence.get(pair)
        relation_edges.append({
            "pair": list(pair),
            "blockers": proposal,
            "confirmed_by_review": pair in ledger_result["relation_assertions"],
            "soft_score": item.soft_score if item else None,
        })

    # 统计只消费完整发布分量：冻结成员与待审对全部排除
    published_records = [record for record in records if record.record_id in published_members]
    kind_of = {record.record_id: record.kind for record in records}
    stats = {
        "records_total": len(records),
        "records_published": len(published_members),
        "records_frozen": len(frozen_set),
        "entity_clusters_published": len(clustering.clusters),
        "multi_record_clusters": sum(1 for members in clustering.clusters.values()
                                     if len(members) > 1),
        "singletons": len(clustering.singletons),
        "by_kind": {
            kind: {
                "records": sum(1 for record in published_records if record.kind == kind),
                "clusters": sum(
                    1 for members in clustering.clusters.values()
                    if all(kind_of[member] == kind for member in members)),
            }
            for kind in ("organization", "facility")
        },
        "review_queue_size": sum(1 for item in evidence.values() if item.decision == "review"),
        "conflict_chains_held": len(clustering.conflict_chains),
        "expired_reviews_held": len(ledger_result["expired"]),
        "relation_edges_total": len(relation_edges),
        "relation_edges_confirmed": sum(1 for item in relation_edges if item["confirmed_by_review"]),
        "basis_note": "统计仅覆盖完整发布的分量；冻结与待审成员不计入",
    }

    body = {
        "schema_version": 1,
        "data_fingerprint": data_fingerprint,
        "policy": policy.as_dict(),
        "record_fingerprints": {record.record_id: record_fingerprint(record)
                                for record in sorted(records, key=lambda r: r.record_id)},
        "clusters": clustering.as_dict()["clusters"],
        "frozen": {
            "members": clustering.frozen_members,
            "conflict_chains": [item.as_dict() for item in clustering.conflict_chains],
        },
        "review_queue": _review_queue(evidence),
        "relations": relation_edges,
        "reviews": {
            "active_same": [edge.as_dict() for edge in clustering.applied_same_edges
                            if edge.origin != "algorithm"],
            "active_different": [edge.as_dict() for edge in clustering.different_edges
                                 if edge.origin != "algorithm"],
            "withdrawn": ledger_result["withdrawn"],
            "expired": ledger_result["expired"],
            "ledger_trace": ledger_result["ledger_trace"],
            "assert_vocabulary": [ASSERT_SAME, ASSERT_DIFFERENT],
        },
        "evidence_index": {f"{a}|{b}": item.as_dict()
                           for (a, b), item in sorted(evidence.items())},
        "candidate_report": candidates.as_dict(),
        "stats": stats,
    }
    body["publication_hash"] = _publication_hash({
        key: value for key, value in body.items() if key != "publication_hash"
    })
    return body
