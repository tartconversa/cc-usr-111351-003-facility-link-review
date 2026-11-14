"""约束消歧工作流的回归检查。

覆盖用户提出的五个对照场景与关键纪律：
同音近名、跨年更名、地址搬迁、集团/基地混淆、桥接误合，
以及撤回重判、证据版本过期、标签隔离、可复现性与候选漏配诊断。
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from src.dedup.blocking import (BLOCKER_NAME_PREFIX, candidate_pairs,
                                all_pairs_for_source_combos, blocking_diagnostics)
from src.dedup.cluster import HUMAN_DIFFERENT, HUMAN_SAME, cluster_records, diff_clusters
from src.dedup.evidence import (DECISION_AUTO_DIFFERENT, DECISION_AUTO_SAME,
                                DECISION_RELATION, DECISION_REVIEW, Policy,
                                build_all_evidence, build_pair_evidence)
from src.dedup.evaluation import evaluate_heldout, tuning_recall_audit
from src.dedup.model import (BundleError, evidence_version, intervals_overlap,
                             load_bundle_file, parse_bundle)
from src.dedup.publish import assemble_publication
from src.dedup.reviews import ReviewError, apply_ledger
from src.dedup.textkit import compare_addresses, name_features, normalize_name

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASES = FIXTURES / "cases.json"
REVIEWED = FIXTURES / "cases_reviewed.json"
REJUDGE = FIXTURES / "cases_rejudge.json"


def _load(path=CASES):
    data = load_bundle_file(path)
    records = parse_bundle(data)
    return data, records


def _run(records, reviews=None, policy=None):
    policy = policy or Policy()
    candidates = candidate_pairs(records)
    evidence = build_all_evidence(records, candidates.identity_pairs, policy)
    evidence.update(build_all_evidence(records, candidates.relation_pairs, policy, relation=True))
    ledger = apply_ledger(records, reviews or [])
    clustering = cluster_records([r.record_id for r in records], evidence, ledger["human_edges"])
    return candidates, evidence, ledger, clustering


class ModelTests(unittest.TestCase):
    def test_missing_registration_normalized_to_none(self):
        data, records = _load()
        self.assertIsNone(next(r for r in records if r.record_id == "B2").registration)
        empty = deepcopy(data)
        empty["records"][0]["registration"] = ""
        records2 = parse_bundle(empty)
        self.assertIsNone(records2[0].registration)

    def test_intervals_overlap(self):
        _, records = _load()
        by_id = {r.record_id: r for r in records}
        self.assertFalse(intervals_overlap(by_id["N1"], by_id["N2"]))
        self.assertTrue(intervals_overlap(by_id["H1"], by_id["H2"]))

    def test_bad_kind_and_duplicate_id_rejected(self):
        data, _ = _load()
        broken = deepcopy(data)
        broken["records"][0]["kind"] = "subsidiary"
        with self.assertRaises(BundleError):
            parse_bundle(broken)
        dup = deepcopy(data)
        dup["records"][1]["record_id"] = dup["records"][0]["record_id"]
        with self.assertRaises(BundleError):
            parse_bundle(dup)

    def test_evidence_version_changes_with_identity_fields(self):
        _, records = _load()
        by_id = {r.record_id: r for r in records}
        version_before = evidence_version(by_id["H1"], by_id["H2"])
        moved = deepcopy(by_id["H1"].raw)
        moved["address"]["detail"] = "99号"
        from src.dedup.model import _check_record
        changed = _check_record(moved, set())
        self.assertNotEqual(evidence_version(changed, by_id["H2"]), version_before)


class SoftEvidenceTests(unittest.TestCase):
    def test_missing_fields_are_not_equality(self):
        features = name_features(None, "某厂")
        self.assertFalse(features.both_present)
        self.assertIsNone(features.char_ratio)
        address = compare_addresses(None, None)
        self.assertEqual(address.level, "unknown")
        self.assertIsNone(address.coverage)

    def test_normalization_never_hard_merges(self):
        # 去空格/全半角折叠后相等，只反映在 normalized_equal 软特征上
        features = name_features("青浦 绿材厂", "青浦绿材厂")
        self.assertTrue(features.normalized_equal)
        self.assertFalse(features.exact_raw_equal)

    def test_address_conflict_and_coverage(self):
        _, records = _load()
        by_id = {r.record_id: r for r in records}
        features = compare_addresses(by_id["M1"].address, by_id["M2"].address)
        self.assertEqual(features.level, "conflict")
        self.assertGreaterEqual(features.differing_fragments, 2)

    def test_homophone_is_weak_explained_evidence(self):
        features = name_features("青浦绿材一厂", "青浦绿才一厂")
        # “材”是等价组规范字，“才”是被替换的非规范字；只报告后者
        self.assertEqual(features.homophone_hits, ("才",))
        self.assertGreaterEqual(features.homophone_ratio, features.char_ratio)


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.data, self.records = _load()
        self.by_id = {r.record_id: r for r in self.records}
        self.candidates, self.evidence, self.ledger, self.clustering = _run(self.records)

    def test_homophone_same_registration_merges_distinct_registration_does_not(self):
        # H1/H2：同音近名 + 同注册号 → 自动同一
        self.assertEqual(self.evidence[("H1", "H2")].decision, DECISION_AUTO_SAME)
        # H1/H3：同时有效且注册号不同 → 硬性不同，名称再像也不合并
        h1h3 = self.evidence[("H1", "H3")]
        self.assertTrue(h1h3.hard_different)
        self.assertEqual(h1h3.decision, DECISION_AUTO_DIFFERENT)
        self.assertIn("H1-registration-conflict-while-overlapping", h1h3.hard_reasons[0])
        cluster_of_h1 = self.clustering.cluster_ids_by_record["H1"]
        self.assertNotIn("H3", self.clustering.clusters[cluster_of_h1])

    def test_cross_year_rename_goes_to_review_not_auto_merge(self):
        n1n2 = self.evidence[("N1", "N2")]
        self.assertEqual(n1n2.decision, DECISION_REVIEW)
        self.assertFalse(n1n2.overlap)
        # 人工确认同一后才合并
        ledger = apply_ledger(self.records, self._review("RV", "assert_same", "N1", "N2"))
        clustering = cluster_records([r.record_id for r in self.records],
                                     self.evidence, ledger["human_edges"])
        self.assertEqual(clustering.cluster_ids_by_record["N1"],
                         clustering.cluster_ids_by_record["N2"])

    def test_relocation_same_registration_address_conflict_holds_for_review(self):
        m1m2 = self.evidence[("M1", "M2")]
        self.assertEqual(m1m2.address_level, "conflict")
        self.assertEqual(m1m2.decision, DECISION_REVIEW)
        self.assertIn("S0-registration-equal", m1m2.decision_basis)

    def test_group_and_base_are_relation_not_identity(self):
        # G1 机构 vs G2 基地：跨类型，硬性不可合并，只产出归属候选
        g1g2 = self.evidence[("G1", "G2")]
        self.assertEqual(g1g2.decision, DECISION_RELATION)
        self.assertTrue(g1g2.relation_proposal)
        self.assertTrue(g1g2.hard_different)
        # 两个名字相似的基地 G2/G3：不同注册号 → 硬不同，不会被相似规则合成一个
        g2g3 = self.evidence[("G2", "G3")]
        self.assertTrue(g2g3.hard_different)
        self.assertEqual(g2g3.decision, DECISION_AUTO_DIFFERENT)
        self.assertNotIn(("G2", "G3"),
                         {tuple(m) for members in self.clustering.clusters.values()
                          for i, m in enumerate([members]) for m in [tuple(members)]})

    def test_bridge_mismerge_is_frozen_not_silently_merged(self):
        # B1≈B2 自动同一、B2≈B3 自动同一，但 B1/B3 注册号硬冲突 → 分量冻结
        self.assertEqual(self.evidence[("B1", "B2")].decision, DECISION_AUTO_SAME)
        self.assertTrue(self.evidence[("B1", "B3")].hard_different)
        self.assertEqual(set(self.clustering.frozen_members), {"B1", "B2", "B3"})
        self.assertTrue(self.clustering.conflict_chains)
        for member in ("B1", "B2", "B3"):
            self.assertNotIn(member, self.clustering.cluster_ids_by_record)

    def test_soft_score_never_drives_hard_decision(self):
        # B1/B3 名称极像（高分软证据）但注册号硬冲突，判定必须是 auto_different
        pair = self.evidence[("B1", "B3")]
        self.assertIsNotNone(pair.soft_score)
        self.assertGreater(pair.soft_score, 0.5)
        self.assertEqual(pair.decision, DECISION_AUTO_DIFFERENT)

    def _review(self, review_id, event_type, a, b, version=None):
        if version is None:
            version = evidence_version(self.by_id[a], self.by_id[b])
        return [{"review_id": review_id, "event_type": event_type,
                 "record_a": a, "record_b": b, "evidence_version": version,
                 "note": "test", "source_version": "v"}]


class CandidateTests(unittest.TestCase):
    def test_provenance_and_blocking_diagnostics(self):
        data, records = _load()
        candidates = candidate_pairs(records)
        self.assertTrue(candidates.identity_pairs)
        for pair in candidates.identity_pairs:
            self.assertTrue(candidates.pair_provenance[pair])
        diagnostics = blocking_diagnostics(
            records, [entry["pair"] for entry in data["labels"]["tuning"]
                      if entry["label"] == "same"])
        self.assertEqual(diagnostics["missing_pairs"], 0)
        self.assertEqual(diagnostics["covered_pairs"], diagnostics["true_pairs"])

    def test_all_pairs_for_source_combos_respects_kind(self):
        _, records = _load()
        pairs = all_pairs_for_source_combos(records, [("catalog", "expo")])
        by_id = {r.record_id: r for r in records}
        for left, right in pairs:
            self.assertEqual(by_id[left].kind, by_id[right].kind)
            self.assertEqual(tuple(sorted((by_id[left].source, by_id[right].source))),
                             ("catalog", "expo"))

    def test_diagnostics_explains_miss_reason(self):
        # 构造一个前缀阻断必然漏配的对：名称完全不同但标签称同一
        data, records = _load()
        diagnostics = blocking_diagnostics(records, [("H1", "X1")])
        self.assertEqual(diagnostics["missing_pairs"], 1)
        reason = diagnostics["missing_detail"][0]["reasons"][BLOCKER_NAME_PREFIX]
        self.assertEqual(reason, "prefix_differs")


class ReviewLedgerTests(unittest.TestCase):
    def test_active_reviews_merge_and_affiliation_separated(self):
        data, records = _load(REVIEWED)
        _, evidence, ledger, clustering = _run(records, data["reviews"])
        self.assertEqual(clustering.cluster_ids_by_record["N1"],
                         clustering.cluster_ids_by_record["N2"])
        self.assertEqual(clustering.cluster_ids_by_record["M1"],
                         clustering.cluster_ids_by_record["M2"])
        # 归属断言不产生实体合并
        self.assertNotEqual(clustering.cluster_ids_by_record.get("G1"),
                            clustering.cluster_ids_by_record.get("G2"))
        self.assertIn(("G1", "G2"), ledger["relation_assertions"])

    def test_withdrawal_reopens_component_without_drifting_other_ids(self):
        before_data, before_records = _load(REVIEWED)
        _, _, _, before = _run(before_records, before_data["reviews"])
        after_data, after_records = _load(REJUDGE)
        _, _, after_ledger, after = _run(after_records, after_data["reviews"])
        self.assertTrue(any(item["target_review_id"] == "RV-002"
                            for item in after_ledger["withdrawn"]))
        # 撤回后 M1/M2 不再同簇
        self.assertNotEqual(after.cluster_ids_by_record.get("M1"),
                            after.cluster_ids_by_record.get("M2"))
        changes = diff_clusters(before.clusters, after.clusters)
        changed_members = {m for item in changes["changed_clusters"]
                           for m in item["old_members"]}
        self.assertEqual(changed_members, {"M1", "M2"})
        # 无关簇标识零漂移
        for members, cluster_id in before.clusters.items():
            pass
        shared = set(before.clusters) & set(after.clusters)
        for cluster_id in shared:
            self.assertEqual(before.clusters[cluster_id], after.clusters[cluster_id])

    def test_expired_review_never_overwrites_new_data(self):
        data, records = _load(REJUDGE)
        ledger = apply_ledger(records, data["reviews"])
        expired = {item["review_id"] for item in ledger["expired"]}
        self.assertIn("RV-004", expired)
        # 过期断言不得形成人工边
        self.assertNotIn(("X1", "X2"), ledger["human_edges"])

    def test_withdraw_without_target_rejected(self):
        _, records = _load()
        with self.assertRaises(ReviewError):
            apply_ledger(records, [{
                "review_id": "W1", "event_type": "withdraw",
                "record_a": "H1", "record_b": "H2",
                "evidence_version": None, "note": "x", "source_version": "v"}])

    def test_human_same_chain_against_human_different_freezes_component(self):
        data, records = _load()
        by_id = {r.record_id: r for r in records}
        def ev(a, b):
            return evidence_version(by_id[a], by_id[b])
        reviews = [
            {"review_id": "K1", "event_type": "assert_same", "record_a": "B1",
             "record_b": "B2", "evidence_version": ev("B1", "B2"), "note": "", "source_version": "v"},
            {"review_id": "K2", "event_type": "assert_same", "record_a": "B2",
             "record_b": "B3", "evidence_version": ev("B2", "B3"), "note": "", "source_version": "v"},
            {"review_id": "K3", "event_type": "assert_different", "record_a": "B1",
             "record_b": "B3", "evidence_version": ev("B1", "B3"), "note": "", "source_version": "v"},
        ]
        _, _, ledger, clustering = _run(records, reviews)
        self.assertEqual(set(clustering.frozen_members), {"B1", "B2", "B3"})
        chains = clustering.conflict_chains
        self.assertTrue(any("B1" in chain.chain and "B2" in chain.chain and "B3" in chain.chain
                            for chain in chains))
        self.assertTrue(any(chain.blocking_edge.origin == "review:K3" for chain in chains))
        # 人工意见一条都没被丢弃
        edge_kinds = {edge.origin for edge in clustering.different_edges}
        self.assertIn("review:K3", edge_kinds)


class PublicationAndEvaluationTests(unittest.TestCase):
    def test_publication_reproducible_and_stats_exclude_frozen(self):
        data, records = _load()
        candidates, evidence, ledger, clustering = _run(records, data["reviews"])
        from src.dedup.workflow import _data_fingerprint
        policy = Policy()
        pub1 = assemble_publication(records, policy, candidates, evidence, clustering,
                                    ledger, _data_fingerprint(records))
        pub2 = assemble_publication(records, policy, candidates, evidence, clustering,
                                    ledger, _data_fingerprint(records))
        self.assertEqual(pub1["publication_hash"], pub2["publication_hash"])
        self.assertEqual(pub1["stats"]["records_published"]
                         + pub1["stats"]["records_frozen"], len(records))
        self.assertNotIn("B1", json.dumps(pub1["clusters"]))

    def test_heldout_isolation_rejects_leakage(self):
        data, records = _load()
        _, evidence, _, clustering = _run(records)
        leaked_policy = Policy(tuned_on_pairs=(("X1", "X2"),))
        with self.assertRaises(ValueError):
            evaluate_heldout(records, data["labels"], leaked_policy, evidence, clustering)
        # 干净政策下留存集可评估，且不允许调参集/留存集重叠
        result = evaluate_heldout(records, data["labels"], Policy(), evidence, clustering)
        self.assertEqual(result["candidate_recall"], 1.0)
        self.assertEqual(result["false_merge_rate"], 0.0)
        overlap_labels = deepcopy(data["labels"])
        overlap_labels["heldout"].append({"pair": ["H1", "H2"], "label": "same"})
        with self.assertRaises(ValueError):
            tuning_recall_audit(records, overlap_labels)

    def test_tuning_audit_reports_recall(self):
        data, records = _load()
        audit = tuning_recall_audit(records, data["labels"])
        self.assertEqual(audit["candidate_recall"], 1.0)
        self.assertIn("blocking", audit)

    def test_tuning_audit_flags_false_merges(self):
        data, records = _load()
        by_id = {r.record_id: r for r in records}
        # 构造一对算法会 auto_same、但人工标签判 different 的记录（名称地址全同、同注册号），
        # 用于验证调参侧能抓到误合；该对不放进留存集，保持隔离纪律。
        clone = deepcopy(by_id["X1"].raw)
        clone["record_id"] = "X3"
        from src.dedup.model import _check_record
        records_with_clone = records + [_check_record(clone, {r.record_id for r in records})]
        labels = deepcopy(data["labels"])
        labels["tuning"].append({"pair": ["X1", "X3"], "label": "different"})
        _, evidence, _, clustering = _run(records_with_clone)
        audit = tuning_recall_audit(records_with_clone, labels,
                                    evidence=evidence, clustering=clustering)
        # 4 个调参 different 对中仅构造对被误合 → 误合率 1/4，清单精确指向该对
        self.assertEqual(audit["merge_audit"]["label_different_pairs"], 4)
        self.assertEqual(audit["merge_audit"]["false_merge_rate"], 0.25)
        self.assertEqual(audit["merge_audit"]["wrongly_merged_pairs"], [["X1", "X3"]])


if __name__ == "__main__":
    unittest.main()
