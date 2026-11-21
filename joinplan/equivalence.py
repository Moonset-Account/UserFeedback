"""Equivalence-class bookkeeping for join keys.

Columns joined with an equality predicate become provably equal; that
equality is transitive, so a relation carries a partition of its columns
into equivalence classes.  The planner uses these classes to decide
whether a redirection step is needed and to label the partitioning of an
intermediate result.
"""

from __future__ import annotations

from typing import Iterable

EqRelation = frozenset[frozenset[str]]


def singleton_relation(columns: Iterable[str]) -> EqRelation:
    """Every column is only equal to itself."""
    return frozenset(frozenset((column,)) for column in columns)


def merge(
    eq: EqRelation,
    left_column: str,
    right_column: str,
    extra: Iterable[str] = (),
) -> EqRelation:
    """Return ``eq`` with the classes of both columns unioned.

    ``extra`` columns are introduced as singletons first (used to add a
    newly joined table's columns to the universe).  Missing columns are
    likewise introduced on demand, so this is safe to call on a relation
    built from a narrower column universe.
    """
    classes = set(eq)
    for column in extra:
        if _find(classes, column) is None:
            classes.add(frozenset((column,)))
    if left_column == right_column:
        return frozenset(classes)
    left_class = _find(classes, left_column)
    right_class = _find(classes, right_column)
    if left_class is None:
        left_class = frozenset((left_column,))
    if right_class is None:
        right_class = frozenset((right_column,))
    classes.discard(left_class)
    classes.discard(right_class)
    classes.add(left_class | right_class)
    return frozenset(classes)


def class_of(eq: EqRelation, column: str) -> frozenset[str]:
    """The equivalence class containing ``column`` (itself if unknown)."""
    found = _find(eq, column)
    return found if found is not None else frozenset((column,))


def union_relations(*relations: EqRelation) -> EqRelation:
    """Union relations over disjoint column universes (different tables)."""
    result: set[frozenset[str]] = set()
    for relation in relations:
        result.update(relation)
    return frozenset(result)


def _find(classes: Iterable[frozenset[str]], column: str) -> frozenset[str] | None:
    for candidate in classes:
        if column in candidate:
            return candidate
    return None
