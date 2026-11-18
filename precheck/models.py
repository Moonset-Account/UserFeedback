"""快照数据模型与模式（pattern）运算。

所有资源模式统一归一化为“基础前缀”：去掉末尾的 ``*`` 通配符后，
若字符串 X 以基础前缀开头，则认为该模式覆盖 X。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def pattern_base(pattern: str) -> str:
    """将资源模式归一化为基础前缀。"""
    base = pattern.rstrip("*")
    return base


def covers(pattern: str, key: str) -> bool:
    """判断模式是否覆盖给定键/前缀。"""
    return key.startswith(pattern_base(pattern))


def intersect(a: str, b: str) -> str | None:
    """两个基础前缀的交集；不相交时返回 None。"""
    if a.startswith(b):
        return a
    if b.startswith(a):
        return b
    return None


@dataclass(frozen=True)
class Principal:
    """策略主体。kind 为 anonymous 或 service。"""

    kind: str
    service: str = ""
    endpoint: str = ""

    @staticmethod
    def parse(raw: Any) -> "Principal":
        if raw in ("*", {"AWS": "*"}, {"type": "anonymous"}):
            return Principal(kind="anonymous")
        if isinstance(raw, dict) and raw.get("type") == "service":
            return Principal(
                kind="service",
                service=str(raw.get("service", "")),
                endpoint=str(raw.get("endpoint", "")),
            )
        raise ValueError(f"无法识别的 principal: {raw!r}")


@dataclass(frozen=True)
class Statement:
    """单条策略语句（组织边界或资源策略）。"""

    sid: str
    effect: str  # "Allow" | "Deny"
    principal: Principal
    actions: tuple[str, ...]
    resources: tuple[str, ...]  # 资源模式
    condition_attributes: tuple[str, ...] = ()
    source: str = ""  # "org_boundary" | "resource_policy:<bucket>"

    def matches_action(self, action: str) -> bool:
        for pat in self.actions:
            if pat == "*" or pat == action:
                return True
            if pat.endswith("*") and action.startswith(pat[:-1]):
                return True
        return False


@dataclass(frozen=True)
class Defaults:
    """继承的默认设置。"""

    block_public_access: bool = True
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    approved_distribution_endpoints: tuple[str, ...] = ()
    known_attributes: tuple[str, ...] = ()

    def effective_block_public(self, bucket: str) -> tuple[bool, str]:
        """返回某桶生效的 block_public_access 及依据说明。"""
        override = self.overrides.get(bucket)
        if override is not None and "block_public_access" in override:
            return bool(override["block_public_access"]), (
                f"资源级覆盖 block_public_access={override['block_public_access']}"
            )
        return self.block_public_access, (
            f"继承默认 block_public_access={self.block_public_access}"
        )


@dataclass(frozen=True)
class Snapshot:
    """一次离线预检所需的完整快照。"""

    defaults: Defaults
    org_boundary: tuple[Statement, ...]
    resource_policies: tuple[Statement, ...]
    prefixes: tuple[str, ...]
