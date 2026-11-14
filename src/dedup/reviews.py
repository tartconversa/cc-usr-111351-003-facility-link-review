"""人工审阅账本：断言、撤回与证据版本过期处理。

人工审阅是与算法分数完全独立的事实线：
- 每条断言/确认不同都绑定明确的记录对、审阅编号与证据版本；
- 证据版本由记录身份字段内容派生，资料更新后版本不匹配的旧审阅进入 expired，
  不会静默覆盖新版本资料，也不会被删除，等待审阅者重新确认；
- 撤回（withdraw）只作用于同一 review_id 的有效断言，撤回事件同样留痕；
- 人工同一链与硬性不同相撞时由 cluster 模块列出冲突链并冻结分量，
  本模块不丢弃任何一条人工意见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .blocking import pair_key
from .cluster import Edge, HUMAN_DIFFERENT, HUMAN_SAME
from .model import Record, evidence_version

ASSERT_SAME = "assert_same"
ASSERT_DIFFERENT = "assert_different"
WITHDRAW = "withdraw"
EVENT_TYPES = (ASSERT_SAME, ASSERT_DIFFERENT, WITHDRAW)
RELATION_ASSERT = "assert_affiliation"
RELATION_WITHDRAW = "withdraw_affiliation"


@dataclass(frozen=True)
class ReviewEvent:
    review_id: str
    event_type: str
    record_a: str
    record_b: str
    evidence_version: str | None
    note: str
    source_version: str
    target_review_id: str = ""

    @property
    def pair(self) -> tuple[str, str]:
        return pair_key(self.record_a, self.record_b)


class ReviewError(ValueError):
    """审阅事件结构或账本状态不满足契约。"""


def _require(value: Any, predicate: bool, message: str) -> Any:
    if not predicate:
        raise ReviewError(message)
    return value


def parse_review_events(raw_events: list[Mapping[str, Any]],
                        records: list[Record]) -> list[ReviewEvent]:
    """校验审阅事件的结构、引用完整性与事件词汇。"""
    by_id = {record.record_id: record for record in records}
    parsed: list[ReviewEvent] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, Mapping):
            raise ReviewError(f"reviews[{index}] 必须为对象")
        review_id = _require(raw.get("review_id"), type(raw.get("review_id")) is str
                             and bool(raw.get("review_id")),
                             f"reviews[{index}] 缺少非空 review_id")
        if review_id in seen_ids:
            raise ReviewError("review_id 重复：" + review_id)
        seen_ids.add(review_id)
        event_type = raw.get("event_type")
        if event_type not in EVENT_TYPES + (RELATION_ASSERT, RELATION_WITHDRAW):
            raise ReviewError(f"审阅{review_id}的event_type非法：{event_type!r}")
        pair_values = (raw.get("record_a"), raw.get("record_b"))
        for side, value in zip(("record_a", "record_b"), pair_values):
            if type(value) is not str or value not in by_id:
                raise ReviewError(f"审阅{review_id}的{side}未指向有效记录")
        if pair_values[0] == pair_values[1]:
            raise ReviewError(f"审阅{review_id}的记录对不能是同一条记录")
        version = raw.get("evidence_version")
        if version is not None and type(version) is not str:
            raise ReviewError(f"审阅{review_id}的evidence_version必须为字符串或null")
        note = raw.get("note", "")
        if type(note) is not str:
            raise ReviewError(f"审阅{review_id}的note必须为字符串")
        target_review_id = raw.get("target_review_id", review_id)
        if type(target_review_id) is not str or not target_review_id:
            raise ReviewError(f"审阅{review_id}的target_review_id必须为非空字符串")
        source_version = raw.get("source_version", "")
        if type(source_version) is not str:
            raise ReviewError(f"审阅{review_id}的source_version必须为字符串")
        parsed.append(ReviewEvent(review_id, event_type, pair_values[0], pair_values[1],
                                  version, note, source_version, target_review_id))
    return parsed


def _current_versions(records: list[Record]) -> dict[tuple[str, str], str]:
    by_id = {record.record_id: record for record in records}
    versions: dict[tuple[str, str], str] = {}
    for index, left in enumerate(records):
        for right in records[index + 1:]:
            versions[pair_key(left.record_id, right.record_id)] = evidence_version(left, right)
    assert len(by_id) == len(records)
    return versions


def apply_ledger(records: list[Record],
                 raw_events: list[Mapping[str, Any]]) -> dict:
    """按事件顺序回放账本，输出当前有效人工边、撤回记录与过期审阅。

    返回：
    - human_edges: 当前有效且证据版本匹配的实体同一/不同边（供聚类）；
    - relation_assertions: 当前有效的跨类型归属断言；
    - withdrawn: 被撤回事件抵消的断言（含 review_id 与原因）；
    - expired: 证据版本与当前资料不一致的断言，需重新确认，不参与聚类；
    - ledger_trace: 完整事件回放轨迹，保证每条人工意见可追溯。
    """
    events = parse_review_events(raw_events, records)
    current_versions = _current_versions(records)
    by_id = {record.record_id: record for record in records}

    active: dict[str, ReviewEvent] = {}
    withdrawn: list[dict] = []
    trace: list[dict] = []
    for event in events:
        entry = {"review_id": event.review_id, "event_type": event.event_type,
                 "pair": list(event.pair)}
        if event.event_type in (WITHDRAW, RELATION_WITHDRAW):
            target = active.pop(event.target_review_id, None)
            if target is None:
                raise ReviewError(
                    f"撤回事件{event.review_id}没有对应的有效断言"
                    f"（target_review_id={event.target_review_id}：不存在、已撤回或已过期）")
            want_relation = event.event_type == RELATION_WITHDRAW
            is_relation = target.event_type == RELATION_ASSERT
            if want_relation != is_relation:
                raise ReviewError(
                    f"撤回事件{event.review_id}与目标断言{event.target_review_id}类型不匹配")
            withdrawn.append({"review_id": event.review_id,
                              "target_review_id": event.target_review_id,
                              "withdrawn_event": target.event_type,
                              "pair": list(target.pair),
                              "note": event.note})
            entry["status"] = "withdrawn"
            entry["target_review_id"] = event.target_review_id
        else:
            if event.review_id in active:
                raise ReviewError(f"review_id {event.review_id} 已存在有效断言，"
                                  "撤回前不能再次断言")
            active[event.review_id] = event
            entry["status"] = "active"
        trace.append(entry)

    human_edges: dict[tuple[str, str], Edge] = {}
    relation_assertions: dict[tuple[str, str], dict] = {}
    expired: list[dict] = []
    for review_id, event in sorted(active.items()):
        current_version = current_versions.get(event.pair)
        if event.evidence_version is not None and current_version != event.evidence_version:
            expired.append({
                "review_id": review_id,
                "event_type": event.event_type,
                "pair": list(event.pair),
                "bound_version": event.evidence_version,
                "current_version": current_version,
                "note": event.note,
                "reason": "evidence_version_mismatch",
            })
            continue
        if event.event_type == ASSERT_SAME:
            kind = HUMAN_SAME
            human_edges[event.pair] = Edge(pair=event.pair, kind=kind,
                                           origin=f"review:{review_id}",
                                           basis=("HUMAN-assert_same",))
        elif event.event_type == ASSERT_DIFFERENT:
            kind = HUMAN_DIFFERENT
            human_edges[event.pair] = Edge(pair=event.pair, kind=kind,
                                           origin=f"review:{review_id}",
                                           basis=("HUMAN-assert_different",))
        elif event.event_type == RELATION_ASSERT:
            record_a, record_b = by_id[event.pair[0]], by_id[event.pair[1]]
            if record_a.kind == record_b.kind:
                raise ReviewError(
                    f"归属断言{review_id}必须跨类型（机构↔基地），同类型请用实体断言")
            relation_assertions[event.pair] = {
                "review_id": review_id,
                "pair": list(event.pair),
                "evidence_version": current_version,
                "note": event.note,
            }

    return {
        "human_edges": human_edges,
        "relation_assertions": relation_assertions,
        "withdrawn": withdrawn,
        "expired": expired,
        "ledger_trace": trace,
    }
