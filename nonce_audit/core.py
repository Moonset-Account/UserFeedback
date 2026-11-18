"""核心核查逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Iterable, Mapping, Sequence


class AuditInputError(ValueError):
    """输入记录结构非法（缺少必填字段或字段类型错误）。"""


@dataclass(frozen=True)
class EncryptionRecord:
    """一条实际加密操作的回执元数据。

    属性
        operation_id: 实际加密的操作编号。相同编号的重复上报只算一次。
        logical_write_id: 逻辑写入编号；超时重执行可能与首次相同。
        nonce: 随机数标记，相等即视为随机数取值相同。
        key_alias: 本次加密实际使用的密钥别名。
        mapping_version: 本次加密生效的密钥等价关系版本。
        source_id: 该条上报的来源标识（回执文件/通道等），用于追溯。
    """

    operation_id: str
    logical_write_id: str
    nonce: str
    key_alias: str
    mapping_version: str
    source_id: str


@dataclass(frozen=True)
class EquivalenceTable:
    """密钥材料等价关系表。

    versions[version] 为 ``{密钥别名: 不透明材料标识}``。同一材料标识即表示
    别名背后是同一份密钥材料，需要合并判断。换钥成功后应使用新的版本。
    材料标识仅用于库内归并，不会出现在 ``audit`` 的输出中。
    """

    versions: Mapping[str, Mapping[str, str]]

    @classmethod
    def from_alias_groups(
        cls, alias_groups: Mapping[str, Sequence[Sequence[str]]]
    ) -> "EquivalenceTable":
        """从按版本分组的别名等价类构造等价关系表。

        参数
            alias_groups: ``{版本: [[别名1, 别名2], [别名3], ...]}``，
                同一内层序列中的别名被视为指向同一份密钥材料。
        """
        versions: dict[str, dict[str, str]] = {}
        for version, groups in alias_groups.items():
            mapping: dict[str, str] = {}
            for index, group in enumerate(groups):
                material_id = f"material-{version}-{index}"
                for alias in group:
                    mapping[alias] = material_id
            versions[str(version)] = mapping
        return cls(versions=versions)


@dataclass(frozen=True)
class ReusePair:
    """需要复核的随机数复用操作对。"""

    operation_id_a: str
    operation_id_b: str
    logical_write_ids: tuple[str, ...]
    nonce: str
    key_class: str
    mapping_version: str
    source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id_a": self.operation_id_a,
            "operation_id_b": self.operation_id_b,
            "logical_write_ids": list(self.logical_write_ids),
            "nonce": self.nonce,
            "key_class": self.key_class,
            "mapping_version": self.mapping_version,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class PendingItem:
    """无法自动定论、需要人工确认的操作。

    出于"不回传密钥内容"的要求，这里不包含密钥别名原文，仅以
    ``key_alias_present`` 表示别名在对应版本中是否存在映射，便于区分
    "版本缺失/别名未登记"与其它待确认原因。
    """

    operation_id: str
    logical_write_id: str
    nonce: str
    mapping_version: str
    reason: str
    source_ids: tuple[str, ...]
    key_alias_present: bool = True
    peer_operation_ids: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "logical_write_id": self.logical_write_id,
            "nonce": self.nonce,
            "mapping_version": self.mapping_version,
            "reason": self.reason,
            "source_ids": list(self.source_ids),
            "key_alias_present": self.key_alias_present,
            "peer_operation_ids": list(self.peer_operation_ids),
        }


@dataclass(frozen=True)
class AuditResult:
    """核查结论。"""

    reuse_pairs: tuple[ReusePair, ...]
    pending: tuple[PendingItem, ...]
    deduplicated_reports: int
    unique_operations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "reuse_pairs": [pair.to_dict() for pair in self.reuse_pairs],
            "pending": [item.to_dict() for item in self.pending],
            "deduplicated_reports": self.deduplicated_reports,
            "unique_operations": self.unique_operations,
        }

    def has_findings(self) -> bool:
        return bool(self.reuse_pairs) or bool(self.pending)


_REQUIRED_TEXT_FIELDS = (
    "operation_id",
    "logical_write_id",
    "nonce",
    "key_alias",
    "mapping_version",
    "source_id",
)


def _coerce_record(raw: Any) -> EncryptionRecord:
    if isinstance(raw, EncryptionRecord):
        return raw
    if not isinstance(raw, Mapping):
        raise AuditInputError(
            f"每条记录必须是字典或 EncryptionRecord，收到 {type(raw).__name__}"
        )
    missing = [name for name in _REQUIRED_TEXT_FIELDS if name not in raw]
    if missing:
        raise AuditInputError(f"记录缺少必填字段: {', '.join(sorted(missing))}")
    values: dict[str, str] = {}
    for name in _REQUIRED_TEXT_FIELDS:
        value = raw[name]
        if not isinstance(value, str) or not value:
            raise AuditInputError(f"字段 {name} 必须是非空字符串")
        values[name] = value
    return EncryptionRecord(**values)


def audit(
    records: Iterable[Any], equivalence: EquivalenceTable | Mapping[str, Any]
) -> AuditResult:
    """对离线加密回执执行随机数复用核查。

    参数
        records: ``EncryptionRecord`` 或同构字典的可迭代对象。
        equivalence: 密钥材料等价关系表（``EquivalenceTable`` 或与其
            ``versions`` 结构相同的映射）。

    返回
        ``AuditResult``，包含需要复核的操作对、待确认项和去重统计。
        输出中只含不透明的密钥材料类标识，不含别名原文、材料标识原文或
        任何密钥内容。
    """
    if isinstance(equivalence, EquivalenceTable):
        table = equivalence
    elif isinstance(equivalence, Mapping):
        table = EquivalenceTable(
            versions={
                str(version): dict(alias_map)
                for version, alias_map in equivalence.items()
            }
        )
    else:
        raise AuditInputError(
            "equivalence 必须是 EquivalenceTable 或版本映射字典"
        )

    # 1. 规范化并按 operation_id 去重；重复上报内容冲突则标记待确认。
    #    source_id 只描述上报来源，同一操作经不同来源重复上报且业务字段
    #    一致时属于正常重复，必须只算一次，因此比对时排除 source_id。
    def _business_key(record: EncryptionRecord) -> tuple[str, str, str, str, str]:
        return (
            record.operation_id,
            record.logical_write_id,
            record.nonce,
            record.key_alias,
            record.mapping_version,
        )

    canonical: dict[str, EncryptionRecord] = {}
    sources: dict[str, set[str]] = {}
    conflicts: set[str] = set()
    total_reports = 0
    for raw in records:
        record = _coerce_record(raw)
        total_reports += 1
        sources.setdefault(record.operation_id, set()).add(record.source_id)
        existing = canonical.get(record.operation_id)
        if existing is None:
            canonical[record.operation_id] = record
        elif _business_key(existing) != _business_key(record):
            conflicts.add(record.operation_id)

    # 2. 解析别名 -> 密钥材料等价类；映射缺失进入待确认。
    #    key_class 使用不透明标识，绝不回传别名或材料标识原文。
    class_index: dict[tuple[str, str], int] = {}
    resolved: dict[str, tuple[str, str, str]] = {}
    unresolved: dict[str, str] = {}
    for operation_id, record in canonical.items():
        if operation_id in conflicts:
            continue
        version_map = table.versions.get(record.mapping_version)
        material_id = (
            version_map.get(record.key_alias) if version_map is not None else None
        )
        if material_id is None:
            unresolved[operation_id] = "mapping_missing"
            continue
        class_key = (record.mapping_version, material_id)
        if class_key not in class_index:
            class_index[class_key] = len(class_index)
        resolved[operation_id] = (
            record.mapping_version,
            f"material-class-{class_index[class_key]}",
            record.nonce,
        )

    # 3. 按 (版本, 密钥材料类, nonce) 分桶，桶内两两操作即为复用对。
    buckets: dict[tuple[str, str, str], list[str]] = {}
    for operation_id, (version, key_class, nonce) in resolved.items():
        buckets.setdefault((version, key_class, nonce), []).append(operation_id)

    reuse_pairs: list[ReusePair] = []
    for (version, key_class, nonce), operation_ids in buckets.items():
        if len(operation_ids) < 2:
            continue
        for op_a, op_b in combinations(sorted(operation_ids), 2):
            record_a = canonical[op_a]
            record_b = canonical[op_b]
            write_ids = sorted(
                {record_a.logical_write_id, record_b.logical_write_id}
            )
            pair_sources = sorted(
                sources[op_a] | sources[op_b]
            )
            reuse_pairs.append(
                ReusePair(
                    operation_id_a=op_a,
                    operation_id_b=op_b,
                    logical_write_ids=tuple(write_ids),
                    nonce=nonce,
                    key_class=key_class,
                    mapping_version=version,
                    source_ids=tuple(pair_sources),
                )
            )
    reuse_pairs.sort(
        key=lambda pair: (
            pair.operation_id_a,
            pair.operation_id_b,
            pair.nonce,
        )
    )

    # 4. 待确认项：
    #    - 映射缺失的操作，关联同版本/同 nonce 的已解析操作（无法确认是否复用）；
    #    - 重复上报互相冲突的操作。
    pending: list[PendingItem] = []
    for operation_id, reason in unresolved.items():
        record = canonical[operation_id]
        peers = sorted(
            other
            for other, (other_version, _other_class, other_nonce) in resolved.items()
            if other_version == record.mapping_version
            and other_nonce == record.nonce
        )
        version_map = table.versions.get(record.mapping_version)
        alias_present = (
            version_map is not None and record.key_alias in version_map
        )
        pending.append(
            PendingItem(
                operation_id=operation_id,
                logical_write_id=record.logical_write_id,
                nonce=record.nonce,
                mapping_version=record.mapping_version,
                reason=reason,
                source_ids=tuple(sorted(sources[operation_id])),
                key_alias_present=alias_present,
                peer_operation_ids=tuple(peers),
            )
        )

    for operation_id in sorted(conflicts):
        record = canonical[operation_id]
        pending.append(
            PendingItem(
                operation_id=operation_id,
                logical_write_id=record.logical_write_id,
                nonce=record.nonce,
                mapping_version=record.mapping_version,
                reason="duplicate_conflict",
                source_ids=tuple(sorted(sources[operation_id])),
            )
        )

    pending.sort(key=lambda item: item.operation_id)

    return AuditResult(
        reuse_pairs=tuple(reuse_pairs),
        pending=tuple(pending),
        deduplicated_reports=total_reports - len(canonical),
        unique_operations=len(canonical),
    )
