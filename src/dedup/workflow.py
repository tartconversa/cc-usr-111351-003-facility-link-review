"""端到端工作流编排与命令行入口。

三个子命令对应三种审阅现场动作：
- candidates: 候选生成报告（含阻断统计、漏配诊断、指定来源组合全对核查）
- publish: 全量证据→聚类→审阅账本→可复现发布包
- rejudge: 带审阅撤回的重判，输出旧簇→新簇成员变化

所有输出写文件而不是打印长 JSON；哈希与稳定排序保证按资料与审阅版本可复现。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

# -I 隔离模式不读取 PYTHONPATH，也不预置 cwd；显式把仓库根加入搜索路径，
# 使 `python -I src/dedup/workflow.py` 在任何 cwd 下都能导入 src 包。
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.dedup.blocking import all_pairs_for_source_combos, candidate_pairs
from src.dedup.cluster import cluster_records, diff_clusters
from src.dedup.evaluation import evaluate_heldout, tuning_recall_audit
from src.dedup.evidence import Policy, build_all_evidence
from src.dedup.model import (canonical_json, load_bundle_file, parse_bundle,
                             record_fingerprint)
from src.dedup.publish import assemble_publication
from src.dedup.reviews import apply_ledger


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
                    encoding="utf-8")


def _data_fingerprint(records) -> str:
    payload = canonical_json([record_fingerprint(record)
                              for record in sorted(records, key=lambda r: r.record_id)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load(path: Path) -> tuple[list, dict]:
    data = load_bundle_file(path)
    records = parse_bundle(data)
    return records, data


def _policy(data: Mapping[str, Any], policy_path: Path | None) -> Policy:
    if policy_path is not None:
        config = json.loads(policy_path.read_text(encoding="utf-8"))
        return Policy.from_config(config)
    return Policy.from_config(data.get("policy"))


def run_candidates(bundle: Path, output: Path, combos: list[tuple[str, str]],
                   labels_path: Path | None) -> None:
    records, data = _load(bundle)
    candidates = candidate_pairs(records)
    report: dict[str, Any] = {"candidates": candidates.as_dict()}
    if combos:
        report["all_pairs_audit"] = {
            "source_combos": [list(combo) for combo in combos],
            "pairs": [list(pair) for pair in all_pairs_for_source_combos(records, combos)],
        }
    if labels_path is not None:
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        policy = _policy(data, None)
        evidence = build_all_evidence(records, candidates.identity_pairs, policy)
        ledger = apply_ledger(records, data.get("reviews", []))
        clustering = cluster_records([record.record_id for record in records],
                                     evidence, ledger["human_edges"])
        report["tuning_blocking_audit"] = tuning_recall_audit(
            records, labels, evidence=evidence, clustering=clustering)
    _write_json(output, report)
    print(f"候选报告已写出：{output}（实体候选 {len(candidates.identity_pairs)} 对，"
          f"归属候选 {len(candidates.relation_pairs)} 对）")


def run_publish(bundle: Path, output: Path, policy_path: Path | None,
                labels_path: Path | None) -> None:
    records, data = _load(bundle)
    policy = _policy(data, policy_path)
    candidates = candidate_pairs(records)
    evidence = build_all_evidence(records, candidates.identity_pairs, policy)
    evidence.update(build_all_evidence(records, candidates.relation_pairs, policy,
                                       relation=True))
    ledger = apply_ledger(records, data.get("reviews", []))
    clustering = cluster_records([record.record_id for record in records],
                                 evidence, ledger["human_edges"])
    publication = assemble_publication(
        records, policy, candidates, evidence, clustering, ledger,
        _data_fingerprint(records))
    _write_json(output, publication)
    print(f"发布包已写出：{output}")
    print(f"  publication_hash={publication['publication_hash'][:12]}… "
          f"已发布簇 {len(clustering.clusters)}，冻结成员 {len(clustering.frozen_members)}，"
          f"待审 {publication['stats']['review_queue_size']} 对，"
          f"过期审阅 {len(ledger['expired'])} 条")
    if labels_path is not None:
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        evaluation = evaluate_heldout(records, labels, policy, evidence, clustering)
        eval_path = output.with_name(output.stem + "_heldout.json")
        _write_json(eval_path, evaluation)
        print(f"留存集评估已写出：{eval_path}（误合率 {evaluation['false_merge_rate']}）")


def run_rejudge(bundle: Path, previous_publication: Path, output: Path,
                policy_path: Path | None) -> None:
    """撤回驱动的重判：对比旧发布包，输出旧簇→新簇成员变化并校验无关分量稳定。"""
    records, data = _load(bundle)
    previous = json.loads(previous_publication.read_text(encoding="utf-8"))
    policy = _policy(data, policy_path)
    candidates = candidate_pairs(records)
    evidence = build_all_evidence(records, candidates.identity_pairs, policy)
    evidence.update(build_all_evidence(records, candidates.relation_pairs, policy,
                                       relation=True))
    ledger = apply_ledger(records, data.get("reviews", []))
    clustering = cluster_records([record.record_id for record in records],
                                 evidence, ledger["human_edges"])
    publication = assemble_publication(
        records, policy, candidates, evidence, clustering, ledger,
        _data_fingerprint(records))

    before_clusters = previous.get("clusters", {})
    changes = diff_clusters(before_clusters, clustering.clusters)
    # 无关分量稳定性：成员集合未变的簇，标识必须保持不变
    before_lookup = {tuple(sorted(members)): cluster_id
                     for cluster_id, members in before_clusters.items()}
    stable_check: list[dict] = []
    for cluster_id, members in clustering.clusters.items():
        key = tuple(sorted(members))
        old_id = before_lookup.get(key)
        if old_id is not None and old_id != cluster_id:
            stable_check.append({"members": list(key), "old": old_id, "new": cluster_id})
    publication["rejudge"] = {
        "previous_publication_hash": previous.get("publication_hash"),
        "cluster_changes": changes,
        "unrelated_identifier_drift": stable_check,
        "withdrawn_in_ledger": ledger["withdrawn"],
        "expired_in_ledger": ledger["expired"],
    }
    _write_json(output, publication)
    print(f"重判发布包已写出：{output}")
    print(f"  变化簇 {len(changes['changed_clusters'])} 个，新增簇 "
          f"{len(changes['new_only_clusters'])} 个，无关分量标识漂移 "
          f"{len(stable_check)} 处（应为 0）")


def _split_combos(values: list[str]) -> list[tuple[str, str]]:
    combos: list[tuple[str, str]] = []
    for value in values:
        parts = tuple(part.strip() for part in value.split(","))
        if len(parts) != 2:
            raise argparse.ArgumentTypeError(
                f"来源组合格式应为 source_a,source_b，实际：{value}")
        combos.append(parts)  # type: ignore[arg-type]
    return combos


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_cand = sub.add_parser("candidates", help="生成候选对与阻断诊断")
    p_cand.add_argument("bundle", type=Path)
    p_cand.add_argument("output", type=Path)
    p_cand.add_argument("--all-pairs", action="append", default=[],
                        help="指定来源组合全对核查，可重复，如 --all-pairs catalog,expo")
    p_cand.add_argument("--labels", type=Path, default=None,
                        help="调参标签 JSON，仅用于阻断漏配诊断")

    p_pub = sub.add_parser("publish", help="全量证据→聚类→发布")
    p_pub.add_argument("bundle", type=Path)
    p_pub.add_argument("output", type=Path)
    p_pub.add_argument("--policy", type=Path, default=None)
    p_pub.add_argument("--labels", type=Path, default=None,
                       help="含 heldout 分割的标签 JSON；留存集不得出现在政策 tuned_on_pairs")

    p_re = sub.add_parser("rejudge", help="审阅撤回后的重判与簇变化对比")
    p_re.add_argument("bundle", type=Path)
    p_re.add_argument("previous_publication", type=Path)
    p_re.add_argument("output", type=Path)
    p_re.add_argument("--policy", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "candidates":
        run_candidates(args.bundle, args.output, _split_combos(args.all_pairs), args.labels)
    elif args.command == "publish":
        run_publish(args.bundle, args.output, args.policy, args.labels)
    elif args.command == "rejudge":
        run_rejudge(args.bundle, args.previous_publication, args.output, args.policy)


if __name__ == "__main__":
    main()
