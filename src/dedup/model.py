"""领域模型与资料校验。

本模块只负责把 JSON 资料解析成不可变的 :class:`Record`，并提供：
- 有效时段重叠判断（硬性注册号规则的前提）；
- 记录指纹与证据版本（人工审阅绑定的证据版本由字段内容派生）。

不在这里执行任何匹配、推断或聚类；缺失字段统一保留为 ``None``，
下游任何相似度都不得把缺失当作相等。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

KINDS = ("organization", "facility")
SOURCES = ("catalog", "expo", "park")
DATE_FIELDS = ("valid_from", "valid_to")
IDENTITY_FIELDS = ("name", "address", "registration", "valid_from", "valid_to", "kind")
ADDRESS_FIELDS = ("province", "city", "district", "street", "detail")


class BundleError(ValueError):
    """资料结构或字段不满足契约。"""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("JSON键重复：" + key)
        result[key] = value
    return result


def _nonfinite(_value: str) -> Any:
    raise BundleError("JSON包含非有限数")


def _is_optional_text(value: Any) -> bool:
    return value is None or type(value) is str


def _parse_day(value: Any, field_name: str, record_id: str) -> date | None:
    if value is None:
        return None
    if type(value) is not str:
        raise BundleError(f"记录{record_id}的{field_name}必须为YYYY-MM-DD字符串或null")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise BundleError(f"记录{record_id}的{field_name}日期非法：{value}") from exc
    if parsed.isoformat() != value:
        raise BundleError(f"记录{record_id}的{field_name}必须为YYYY-MM-DD格式：{value}")
    return parsed


def _today(value: Any) -> date | None:
    if value is None:
        return None
    if type(value) is date:
        return value
    return date.fromisoformat(value)


@dataclass(frozen=True)
class Address:
    """可缺失的地址片段。缺失片段是 ``None``，不是空串，禁止按相等处理。"""

    province: str | None = None
    city: str | None = None
    district: str | None = None
    street: str | None = None
    detail: str | None = None

    @classmethod
    def from_raw(cls, value: Any, record_id: str) -> "Address | None":
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise BundleError(f"记录{record_id}的address必须为对象或null")
        unknown = set(value) - set(ADDRESS_FIELDS)
        if unknown:
            raise BundleError(f"记录{record_id}的address含未知片段：{sorted(unknown)}")
        for name in ADDRESS_FIELDS:
            if not _is_optional_text(value.get(name)):
                raise BundleError(f"记录{record_id}的address.{name}必须为字符串或null")
        return cls(**{name: value.get(name) for name in ADDRESS_FIELDS})

    def fragments(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in ADDRESS_FIELDS}


@dataclass(frozen=True)
class Record:
    """一条来源记录。机构与基地通过 ``kind`` 区分，属于不同实体类型。"""

    record_id: str
    source: str
    kind: str
    name: str | None
    registration: str | None
    valid_from: date | None
    valid_to: date | None
    address: Address | None = None
    parent_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def valid_on(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True


def _check_record(raw: Any, seen_ids: set[str]) -> Record:
    if not isinstance(raw, Mapping):
        raise BundleError("records 的每个元素必须为对象")
    record_id = raw.get("record_id")
    if type(record_id) is not str or not record_id:
        raise BundleError("record_id 必须为非空字符串")
    if record_id in seen_ids:
        raise BundleError("record_id 重复：" + record_id)
    seen_ids.add(record_id)

    source = raw.get("source")
    if source not in SOURCES:
        raise BundleError(f"记录{record_id}的source非法：{source!r}，允许 {SOURCES}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise BundleError(f"记录{record_id}的kind非法：{kind!r}，允许 {KINDS}")
    if not _is_optional_text(raw.get("name")):
        raise BundleError(f"记录{record_id}的name必须为字符串或null")
    registration = raw.get("registration")
    if not _is_optional_text(registration):
        raise BundleError(f"记录{record_id}的registration必须为字符串或null")
    elif registration == "":
        # 空串不是一个注册号：规范化为缺失，避免空串之间被当作同一注册号。
        registration = None
    valid_from = _parse_day(raw.get("valid_from"), "valid_from", record_id)
    valid_to = _parse_day(raw.get("valid_to"), "valid_to", record_id)
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        raise BundleError(f"记录{record_id}的有效时段倒置")
    parent_id = raw.get("parent_id")
    if parent_id is not None and type(parent_id) is not str:
        raise BundleError(f"记录{record_id}的parent_id必须为字符串或null")
    address = Address.from_raw(raw.get("address"), record_id)
    return Record(
        record_id=record_id, source=source, kind=kind, name=raw.get("name"),
        registration=registration, valid_from=valid_from, valid_to=valid_to,
        address=address, parent_id=parent_id, raw=dict(raw),
    )


def parse_records(records: Iterable[Mapping[str, Any]]) -> list[Record]:
    """把原始记录字典解析为不可变记录列表，并做结构与唯一性校验。"""
    seen: set[str] = set()
    parsed = [_check_record(item, seen) for item in records]
    if not parsed:
        raise BundleError("主资料集合必须为非空列表：records")
    return parsed


def load_bundle_file(path: str | Path) -> dict[str, Any]:
    """安全读取资料 JSON：拒绝重复键与非有限数。"""
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=_object, parse_constant=_nonfinite)


def parse_bundle(data: Mapping[str, Any]) -> list[Record]:
    """校验资料根结构并返回记录列表。其余字段原样保留在调用方的 data 中。"""
    if not isinstance(data, Mapping):
        raise BundleError("资料根节点必须为对象")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise BundleError("不支持的资料结构版本")
    for name in ("schema_version", "records", "reviews", "field_rules"):
        if name not in data:
            raise BundleError("缺少资料字段：" + name)
    if not isinstance(data["records"], list):
        raise BundleError("主资料集合必须为非空列表：records")
    return parse_records(data["records"])


def intervals_overlap(left: Record, right: Record) -> bool:
    """两条记录的有效时段是否相交。

    开放端（``None``）按无限远处理；单点日也算相交。
    """
    starts = [item.valid_from for item in (left, right) if item.valid_from is not None]
    latest_start = max(starts) if starts else None
    ends = [item.valid_to for item in (left, right) if item.valid_to is not None]
    earliest_end = min(ends) if ends else None
    if latest_start is not None and earliest_end is not None:
        return latest_start <= earliest_end
    return True


def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、无多余空白，作为哈希输入。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _address_identity(address: Address | None) -> Any:
    if address is None:
        return None
    return address.fragments()


def record_identity_payload(record: Record) -> dict[str, Any]:
    """参与证据版本计算的身份字段；raw 中的其他字段不影响证据版本。"""
    return {
        "name": record.name,
        "registration": record.registration,
        "address": _address_identity(record.address),
        "valid_from": record.valid_from.isoformat() if record.valid_from else None,
        "valid_to": record.valid_to.isoformat() if record.valid_to else None,
        "kind": record.kind,
    }


def record_fingerprint(record: Record) -> str:
    """单条记录的内容指纹（含来源与层级，parent_id 不改变实体身份字段也计入数据版本）。"""
    payload = {
        "record_id": record.record_id,
        "source": record.source,
        "identity": record_identity_payload(record),
        "parent_id": record.parent_id,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def evidence_version(record_a: Record, record_b: Record) -> str:
    """人工审阅所绑定的证据版本：只由两条记录的身份字段派生。

    名称、地址、注册号、有效时段或层级任一变化都会产生新版本，
    使旧审阅进入过期队列，而不是静默覆盖新资料。
    政策（权重/阈值）变化不改变证据版本：政策与人工事实是两条独立的版本线。
    """
    payload = canonical_json([
        record_identity_payload(record_a),
        record_identity_payload(record_b),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def as_date(value: Any) -> date | None:
    return _today(value)
