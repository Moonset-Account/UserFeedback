"""Sensitivity labels and process clearance for MAC-style access control.

A label has a hierarchical *level* (a non-negative integer) and a non-hierarchical
set of *categories*.  Label ``a`` *dominates* label ``b`` (``a >= b``) when its
level is at least as high and its categories contain every category of ``b``.

A :class:`Clearance` is the upper bound a process is allowed to reach.  A read
is only permitted when the label resulting from merging the process label and
the object label remains inside (is dominated by) the process clearance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import AbstractSet, Iterable, Union

__all__ = ["Label", "Clearance"]


@dataclass(frozen=True)
class Label:
    """An immutable sensitivity label: ``level`` plus a set of categories."""

    level: int
    categories: frozenset[str] = field(default_factory=frozenset)

    def __init__(
        self,
        level: int,
        categories: Iterable[str] | AbstractSet[str] | None = None,
    ) -> None:
        if not isinstance(level, int) or isinstance(level, bool):
            raise TypeError("level must be an integer")
        if level < 0:
            raise ValueError("level must be non-negative")
        cats = frozenset() if categories is None else frozenset(categories)
        for cat in cats:
            if not isinstance(cat, str):
                raise TypeError("categories must be strings")
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "categories", cats)

    def dominates(self, other: "Label") -> bool:
        """Return ``True`` when this label is at least as sensitive as ``other``."""
        return (
            self.level >= other.level
            and self.categories >= other.categories
        )

    def __ge__(self, other: "Label") -> bool:
        return self.dominates(other)

    def __le__(self, other: "Label") -> bool:
        return other.dominates(self)

    def __gt__(self, other: "Label") -> bool:
        return self.dominates(other) and self != other

    def __lt__(self, other: "Label") -> bool:
        return other.dominates(self) and self != other

    def merge(self, other: "Label") -> "Label":
        """Least-upper-bound: max level and the union of categories."""
        return Label(
            max(self.level, other.level),
            self.categories | other.categories,
        )

    def __or__(self, other: "Label") -> "Label":
        return self.merge(other)

    def with_level(self, level: int) -> "Label":
        return Label(level, self.categories)

    def with_categories(self, categories: Iterable[str]) -> "Label":
        return Label(self.level, categories)

    def __str__(self) -> str:
        cats = ",".join(sorted(self.categories))
        return f"L{self.level}:{{{cats}}}"

    def __repr__(self) -> str:
        return f"Label(level={self.level}, categories={set(self.categories)!r})"


@dataclass(frozen=True)
class Clearance:
    """The maximum label a process is permitted to attain.

    A label is *within* the clearance when the clearance label dominates it.
    """

    bound: Label

    def __init__(
        self,
        bound: Union["Label", int],
        categories: Iterable[str] | AbstractSet[str] | None = None,
    ) -> None:
        if isinstance(bound, Label):
            if categories is not None:
                raise ValueError("do not pass categories together with a Label")
            label = bound
        else:
            label = Label(bound, categories)
        object.__setattr__(self, "bound", label)

    def permits(self, label: Label) -> bool:
        """Return ``True`` when ``label`` does not exceed this clearance."""
        return self.bound.dominates(label)

    def __contains__(self, label: Label) -> bool:
        return self.permits(label)

    def __str__(self) -> str:
        return f"Clearance({self.bound})"

    def __repr__(self) -> str:
        return f"Clearance({self.bound!r})"
