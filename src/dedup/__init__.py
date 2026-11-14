"""约束实体消歧工作流。

模块分层：
- model: 来源记录、地址、有效时段的结构校验与规范化，不做任何实体推断
- textkit: 名称规范化与纯标准库的字符级相似度
- evidence: 成对软证据、硬性规则与可解释的离散判定
- blocking: 候选生成阻断键、漏配诊断与指定来源组合的全对核查
- cluster: 受硬约束保护的全局聚类、冲突链与稳定簇标识
- reviews: 人工审阅账本（断言/撤回/过期），人工事实与算法分数严格分离
- publish: 可复现发布包组装，统计只消费完整发布分量
- evaluation: 调参集/留存集隔离的候选召回与误合率评估
- workflow: 命令行入口
"""

from .model import Record, Address, parse_bundle, parse_records, intervals_overlap
from .evidence import Policy, build_pair_evidence
from .blocking import candidate_pairs, all_pairs_for_source_combos, blocking_diagnostics
from .cluster import cluster_records, diff_clusters
from .reviews import apply_ledger
from .publish import assemble_publication
from .evaluation import tuning_recall_audit, evaluate_heldout

__all__ = [
    "Record", "Address", "parse_bundle", "parse_records", "intervals_overlap",
    "Policy", "build_pair_evidence",
    "candidate_pairs", "all_pairs_for_source_combos", "blocking_diagnostics",
    "cluster_records", "diff_clusters",
    "apply_ledger", "assemble_publication",
    "tuning_recall_audit", "evaluate_heldout",
]
