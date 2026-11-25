"""Permission vocabulary for the host-file sandbox model."""

from __future__ import annotations

from enum import Enum
from typing import Iterable, Set, Union


class Perm(str, Enum):
    """Permissions understood by the model.

    ``READ``, ``WRITE`` and ``EXECUTE`` are the object rights whose escalation a
    subtree move must prevent.  ``MOVE_OUT`` and ``MOVE_IN`` are endpoint
    permissions for cross-directory movement.
    """

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    MOVE_OUT = "move_out"
    MOVE_IN = "move_in"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: "Perm | str") -> "Perm":
        if isinstance(value, Perm):
            return value
        try:
            return cls(value)
        except ValueError:
            # Also accept compact r/w/x letters for readable synthetic fixtures.
            aliases = {
                "r": cls.READ,
                "w": cls.WRITE,
                "x": cls.EXECUTE,
                "out": cls.MOVE_OUT,
                "in": cls.MOVE_IN,
            }
            if value in aliases:
                return aliases[value]
            raise


PermissionLike = Union[Perm, str]
PermissionSet = Set[Perm]

RWX: frozenset[Perm] = frozenset({Perm.READ, Perm.WRITE, Perm.EXECUTE})
ALL_PERMISSIONS: frozenset[Perm] = frozenset(Perm)


def normalize_permissions(permissions: Iterable[PermissionLike] | PermissionLike | None) -> frozenset[Perm]:
    """Convert strings/Perm values to a frozen set of :class:`Perm`."""

    if not permissions:
        return frozenset()
    if isinstance(permissions, (str, Perm)):
        permissions = [permissions]
    return frozenset(Perm.parse(p) for p in permissions)
