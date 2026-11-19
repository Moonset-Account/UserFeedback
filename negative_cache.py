"""专网 DNS 区的否定缓存（NSEC 风格）。

适用环境：
- 区内只使用单层小写 ASCII 标签，没有通配记录；
- 输入记录均已通过验签，本模块只做缓存与证据判定，不做密码学校验。

每条记录包含：所在区（zone）、所有者（owner）、规范序中的下一名称
（next_name）、所有者现有类型集合（types）与失效时间（expires，绝对秒）。

判定语义：
- 按标签字节序，(owner, next_name) 开区间证明区间内的名称不存在；
- 区间起点（owner 本身）只能证明该所有者处某类型缺失；
- next_name <= owner 时区间绕回排序末尾（覆盖 owner 之后与 next_name 之前）；
- 证据严格按 zone 隔离：同名委派点的父区记录不能借给子区使用；
- 缓存中相反的有效证据重叠时，查询返回“待重新核验”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# 单层小写 ASCII 标签：小写字母、数字、连字符；明确排除通配符 "*"。
_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

# 类型助记符：大写字母/数字（如 A、AAAA、NS、SOA、TXT、TYPE65534）。
_TYPE_RE = re.compile(r"^[A-Z0-9]{1,10}$")


class QueryResult(Enum):
    """否定缓存对一次查询的判定。"""

    NAME_MISSING = "name-missing"      # 名称不存在（开区间覆盖）
    TYPE_MISSING = "type-missing"      # 名称存在但该类型不存在（命中起点）
    INSUFFICIENT = "insufficient"      # 证据不足，无法给出否定结论
    REVALIDATE = "revalidate"          # 有效证据相互冲突，需重新核验


def _check_label(value: str, what: str) -> None:
    if not isinstance(value, str) or not _LABEL_RE.match(value):
        raise ValueError(f"{what} 不是合法的单层小写 ASCII 标签: {value!r}")


@dataclass(frozen=True)
class NSECRecord:
    """一条验签通过的否定证据记录。"""

    zone: str                       # 所在区（证据严格按区隔离）
    owner: str                      # 所有者名称（单层标签）
    next_name: str                  # 规范序中的下一名称
    types: frozenset[str]           # 所有者现有类型
    expires: float                  # 失效时间（绝对秒）

    def __post_init__(self) -> None:
        _check_label(self.zone, "zone")
        _check_label(self.owner, "owner")
        _check_label(self.next_name, "next_name")
        if self.owner == self.next_name:
            raise ValueError("owner 与 next_name 相同，无法构成有效区间")
        cleaned = set()
        for t in self.types:
            if not isinstance(t, str) or not _TYPE_RE.match(t):
                raise ValueError(f"非法类型助记符: {t!r}")
            cleaned.add(t)
        if not cleaned:
            raise ValueError("types 不能为空：所有者至少存在一个类型")
        object.__setattr__(self, "types", frozenset(cleaned))
        if not isinstance(self.expires, (int, float)):
            raise ValueError("expires 必须是数值（绝对秒）")

    def is_valid_at(self, now: float) -> bool:
        """记录在 now 时刻是否仍然有效（未失效）。"""
        return now < self.expires

    def is_delegation(self) -> bool:
        """是否为委派点：有 NS 且没有 SOA。"""
        return "NS" in self.types and "SOA" not in self.types

    def covers(self, name: str) -> bool:
        """name 是否落在 (owner, next_name) 开区间内（证明名称不存在）。

        next_name <= owner 时区间绕回排序末尾：
        覆盖 owner 之后直到排序末尾、以及排序开头到 next_name 之前的部分。
        两个端点本身都不被覆盖（开区间）。
        """
        if name == self.owner or name == self.next_name:
            return False
        if self.owner < self.next_name:
            return self.owner < name < self.next_name
        # 绕回：name > owner 或 name < next_name
        return name > self.owner or name < self.next_name


@dataclass
class NegativeCache:
    """否定证据缓存。

    add() 以 (zone, owner) 为键替换同名旧记录；不同 owner 的记录并存，
    若有效区间相互重叠则视为相反证据，查询时返回 REVALIDATE。
    """

    _records: dict[tuple[str, str], NSECRecord] = field(default_factory=dict)

    def add(self, record: NSECRecord, now: float = 0.0) -> None:
        """写入一条验签通过的记录；已失效的记录直接拒绝。"""
        if not record.is_valid_at(now):
            raise ValueError("记录已失效，拒绝写入缓存")
        self._records[(record.zone, record.owner)] = record

    def _valid_records(self, zone: str, now: float) -> list[NSECRecord]:
        return [
            r
            for r in self._records.values()
            if r.zone == zone and r.is_valid_at(now)
        ]

    def query(self, zone: str, name: str, qtype: str, now: float) -> QueryResult:
        """对 (zone, name, qtype) 在 now 时刻做否定判定。

        证据按 zone 严格隔离：同名委派点的父区记录不会用于子区查询，
        反之亦然。
        """
        _check_label(zone, "zone")
        _check_label(name, "name")
        if not _TYPE_RE.match(qtype):
            raise ValueError(f"非法查询类型: {qtype!r}")

        records = self._valid_records(zone, now)
        at_owner = [r for r in records if r.owner == name]
        covering = [r for r in records if r.covers(name)]

        # 相反的有效证据重叠：名称既被开区间覆盖又是某记录起点，
        # 或被两条及以上不同区间同时覆盖。
        if len(covering) > 1 or (covering and at_owner):
            return QueryResult.REVALIDATE

        if at_owner:
            # 命中区间起点：只能证明类型缺失。
            record = at_owner[0]
            if qtype in record.types:
                # 该类型据记录存在，否定缓存无法给出否定结论。
                return QueryResult.INSUFFICIENT
            return QueryResult.TYPE_MISSING

        if covering:
            # 命中开区间：名称不存在。
            return QueryResult.NAME_MISSING

        return QueryResult.INSUFFICIENT
