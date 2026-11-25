"""Policy layers and result objects for permission and move decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .permissions import Perm, normalize_permissions, PermissionLike

PUBLIC_PRINCIPAL = "*"
# A concrete stranger used when checking whether a wildcard grant creates a
# right for principals not explicitly named in the fixture.
STRANGER_PRINCIPAL = "@anonymous-stranger"


@dataclass(frozen=True)
class Rule:
    """An allow rule rooted at an absolute virtual path.

    A rule at ``/a`` applies to ``/a`` and every node below it.  Multiple rules
    in the same policy layer combine by union along the ancestor path.
    """

    path: str
    permissions: frozenset[Perm]
    principal: str = PUBLIC_PRINCIPAL

    def __init__(
        self,
        path: str,
        permissions: Iterable[PermissionLike],
        principal: str = PUBLIC_PRINCIPAL,
    ) -> None:
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "permissions", normalize_permissions(permissions))
        object.__setattr__(self, "principal", principal)


class PolicyLayer:
    """A named policy layer managing a fixed set of permissions."""

    def __init__(
        self,
        name: str,
        managed_permissions: Iterable[PermissionLike],
        rules: Iterable[Rule] | None = None,
    ) -> None:
        self.name = name
        self.managed_permissions = normalize_permissions(managed_permissions)
        self.rules: list[Rule] = list(rules or [])
        for rule in self.rules:
            unmanaged = rule.permissions - self.managed_permissions
            if unmanaged:
                raise ValueError(
                    f"layer {name!r} rule at {rule.path} grants unmanaged permissions: "
                    f"{sorted(p.value for p in unmanaged)}"
                )

    def add_rule(self, rule: Rule) -> "PolicyLayer":
        unmanaged = rule.permissions - self.managed_permissions
        if unmanaged:
            raise ValueError(
                f"layer {self.name!r} does not manage "
                f"{sorted(p.value for p in unmanaged)}"
            )
        self.rules.append(rule)
        return self

    def __repr__(self) -> str:
        return (
            f"PolicyLayer(name={self.name!r}, "
            f"managed={sorted(p.value for p in self.managed_permissions)}, "
            f"rules={len(self.rules)})"
        )


@dataclass(frozen=True)
class LayerDecision:
    layer: str
    permission: Perm
    manages: bool
    accumulated: frozenset[Perm]
    allowed: bool
    matching_rule_paths: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    principal: str
    path: str
    inode: str
    permission: Perm
    allowed: bool
    directory_granted: bool
    layer_decisions: tuple[LayerDecision, ...]
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RightGain:
    inode: str
    principal: str
    path_before: str
    path_after: str
    added: frozenset[Perm]
    rights_before: frozenset[Perm]
    rights_after: frozenset[Perm]


@dataclass(frozen=True)
class MovePlan:
    source_inode: str
    source_path_before: str
    destination_parent_inode: str
    destination_parent_path: str
    final_path: str
    principal: str
    allowed: bool
    source_decision: Decision | None = None
    destination_decision: Decision | None = None
    gains: tuple[RightGain, ...] = field(default_factory=tuple)
    structural_error: str | None = None

    @property
    def endpoint_allowed(self) -> bool:
        return bool(
            self.source_decision
            and self.source_decision.allowed
            and self.destination_decision
            and self.destination_decision.allowed
        )
