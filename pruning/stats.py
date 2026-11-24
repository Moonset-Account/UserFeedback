"""Row-group statistics and confirmed column relationships."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .expressions import canonical_op


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _coerce_bound(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if _is_int(value):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise TypeError(f"{name} must be an integer, got {value!r}")


class ColumnStats:
    """Statistics for one integer column in one row group.

    Parameters
    ----------
    lower, upper:
        Inclusive non-NULL minimum/maximum.  ``None`` means unbounded/unknown.
    may_be_null:
        Whether NULL values are possible.  Defaults to ``True`` so omitted
        nullness cannot cause an unsound whole-group pass.
    all_null:
        Whether every value in the group is NULL.
    """

    def __init__(
        self,
        lower: int | None = None,
        upper: int | None = None,
        may_be_null: bool | None = None,
        all_null: bool | None = None,
        *,
        min: int | None = None,
        max: int | None = None,
        lo: int | None = None,
        hi: int | None = None,
        min_value: int | None = None,
        max_value: int | None = None,
        null_possible: bool | None = None,
        can_be_null: bool | None = None,
        nullable: bool | None = None,
        has_null: bool | None = None,
        contains_null: bool | None = None,
        any_null: bool | None = None,
        is_all_null: bool | None = None,
        all_values_null: bool | None = None,
        null_count: int | None = None,
        row_count: int | None = None,
    ) -> None:
        candidates_lower = [lower, min, lo, min_value]
        candidates_upper = [upper, max, hi, max_value]
        chosen_lower = next((x for x in candidates_lower if x is not None), None)
        chosen_upper = next((x for x in candidates_upper if x is not None), None)

        chosen_lower = _coerce_bound(chosen_lower, "lower bound")
        chosen_upper = _coerce_bound(chosen_upper, "upper bound")

        if chosen_lower is not None and chosen_upper is not None and chosen_lower > chosen_upper:
            raise ValueError(
                f"invalid column bounds: lower={chosen_lower} > upper={chosen_upper}"
            )

        all_null_value = next(
            (x for x in [all_null, is_all_null, all_values_null] if x is not None),
            False,
        )
        if not isinstance(all_null_value, bool):
            raise TypeError("all_null must be a boolean")

        explicit_nullable = next(
            (
                x
                for x in [
                    may_be_null,
                    null_possible,
                    can_be_null,
                    nullable,
                    has_null,
                    contains_null,
                    any_null,
                ]
                if x is not None
            ),
            None,
        )

        if null_count is not None:
            if not _is_int(null_count) or null_count < 0:
                raise ValueError("null_count must be a non-negative integer")
            if row_count is not None:
                if not _is_int(row_count) or row_count < 0:
                    raise ValueError("row_count must be a non-negative integer")
                if null_count > row_count:
                    raise ValueError("null_count cannot exceed row_count")
            if explicit_nullable is None:
                explicit_nullable = null_count > 0
            if all_null is None and row_count is not None and row_count > 0 and null_count == row_count:
                all_null_value = True

        if all_null_value:
            if explicit_nullable is False:
                raise ValueError("all_null column cannot be marked non-nullable")
            explicit_nullable = True

        self.lower = chosen_lower
        self.upper = chosen_upper
        self.may_be_null = True if explicit_nullable is None else bool(explicit_nullable)
        self.all_null = all_null_value

    @property
    def min(self) -> int | None:
        return self.lower

    @property
    def max(self) -> int | None:
        return self.upper

    @property
    def null_possible(self) -> bool:
        return self.may_be_null

    @property
    def is_all_null(self) -> bool:
        return self.all_null

    def __repr__(self) -> str:
        return (
            "ColumnStats("
            f"lower={self.lower!r}, upper={self.upper!r}, "
            f"may_be_null={self.may_be_null!r}, all_null={self.all_null!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ColumnStats):
            return NotImplemented
        return (
            self.lower == other.lower
            and self.upper == other.upper
            and self.may_be_null == other.may_be_null
            and self.all_null == other.all_null
        )

    def __hash__(self) -> int:
        return hash((self.lower, self.upper, self.may_be_null, self.all_null))


def coerce_column_stats(value: Any, column_name: str | None = None) -> ColumnStats:
    if isinstance(value, ColumnStats):
        return value
    if value is None:
        return ColumnStats()
    if isinstance(value, Mapping):
        data = dict(value)
        # Allow {"bounds": [0, 10]} in addition to direct keys.
        bounds = data.pop("bounds", None)
        if bounds is not None and "lower" not in data and "min" not in data:
            if not isinstance(bounds, Sequence) or len(bounds) != 2:
                raise ValueError("bounds must be a (lower, upper) pair")
            data.setdefault("lower", bounds[0])
            data.setdefault("upper", bounds[1])
        try:
            return ColumnStats(**data)
        except TypeError as exc:
            prefix = f"invalid stats for column {column_name!r}: " if column_name else ""
            raise TypeError(prefix + str(exc)) from exc
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 2:
            lower, upper = value
            return ColumnStats(lower, upper)
        if len(value) == 3:
            lower, upper, may_be_null = value
            return ColumnStats(lower, upper, may_be_null)
        if len(value) == 4:
            lower, upper, may_be_null, all_null = value
            return ColumnStats(lower, upper, may_be_null, all_null)
    raise TypeError(f"cannot coerce column stats from {value!r}")


@dataclass(frozen=True)
class Relation:
    """A summary-confirmed relationship between two columns.

    The relation is interpreted row-by-row when both values are non-NULL.
    """

    left: str
    op: str
    right: str

    def __post_init__(self) -> None:
        if not isinstance(self.left, str) or not self.left:
            raise ValueError("relation left column must be a non-empty string")
        if not isinstance(self.right, str) or not self.right:
            raise ValueError("relation right column must be a non-empty string")
        object.__setattr__(self, "op", canonical_op(self.op))

    def __str__(self) -> str:
        return f"{self.left} {self.op} {self.right}"


def coerce_relation(value: Any) -> Relation:
    if isinstance(value, Relation):
        return value
    if isinstance(value, str):
        from .parser import parse_expr

        expr = parse_expr(value)
        from .expressions import Column as ExprColumn
        from .expressions import Compare

        if not isinstance(expr, Compare) or not isinstance(expr.left, ExprColumn) or not isinstance(expr.right, ExprColumn):
            raise ValueError(f"relation string must be column op column: {value!r}")
        return Relation(expr.left.name, expr.op, expr.right.name)
    if isinstance(value, Mapping):
        left = value.get("left") or value.get("lhs") or value.get("column")
        right = value.get("right") or value.get("rhs") or value.get("other")
        op = value.get("op") or value.get("relation") or value.get("operator") or "<"
        if left is None or right is None:
            raise ValueError(f"relation mapping requires left/right: {value!r}")
        return Relation(str(left), str(op), str(right))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) == 3:
            return Relation(str(value[0]), str(value[1]), str(value[2]))
        if len(value) == 2:
            # Convenient ordered-pair form means left < right.
            return Relation(str(value[0]), "<", str(value[1]))
    raise TypeError(f"cannot coerce relation from {value!r}")


def _is_column_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and (value[0].isalpha() or value[0] == "_")
        and all(ch.isalnum() or ch == "_" for ch in value[1:])
    )


def _is_single_relation_sequence(value: Any) -> bool:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return False
    if len(value) == 3 and _is_column_name(value[0]) and _is_column_name(value[2]):
        try:
            canonical_op(value[1])
        except (ValueError, TypeError):
            return False
        return True
    return len(value) == 2 and _is_column_name(value[0]) and _is_column_name(value[1])


def coerce_relations(value: Any) -> tuple[Relation, ...]:
    if value is None:
        return ()
    if isinstance(value, Relation):
        return (value,)
    if _is_single_relation_sequence(value):
        return (coerce_relation(value),)
    if isinstance(value, Mapping):
        # {"a": {"<": "b", "<=": "c"}}
        expanded: list[Any] = []
        for left, ops in value.items():
            if isinstance(ops, Mapping):
                for op, right in ops.items():
                    if isinstance(right, (list, tuple)) and right and isinstance(right[0], (list, tuple, Mapping)):
                        for item in right:
                            expanded.append((left, op, item))
                    elif isinstance(right, (list, tuple)):
                        for item in right:
                            expanded.append((left, op, item))
                    else:
                        expanded.append((left, op, right))
            elif (
                isinstance(ops, Sequence)
                and not isinstance(ops, (str, bytes))
                and len(ops) == 2
                and isinstance(ops[0], str)
                and ops[0] in ("=", "==", "!=", "<>", "<", "<=", ">", ">=", "eq", "ne", "lt", "le", "gt", "ge")
            ):
                # {"x": ("<", "y")}
                expanded.append((str(left), ops[0], ops[1]))
            elif isinstance(ops, Sequence) and not isinstance(ops, (str, bytes)):
                for item in ops:
                    if isinstance(item, Relation):
                        expanded.append(item)
                    elif isinstance(item, Mapping):
                        item = dict(item)
                        item.setdefault("left", left)
                        expanded.append(item)
                    elif isinstance(item, Sequence) and len(item) == 3:
                        expanded.append((str(left), item[1], item[2]))
                    elif isinstance(item, Sequence) and len(item) == 2 and isinstance(item[0], str):
                        expanded.append((str(left), item[0], item[1]))
                    else:
                        expanded.append((left, "<", item))
            else:
                expanded.append((left, "<", ops))
        return tuple(coerce_relation(item) for item in expanded)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) >= 2 and all(_is_column_name(item) for item in value):
            # Flat ascending order: ["x", "y", "z"] means x<y and y<z.
            return tuple(Relation(value[i], "<", value[i + 1]) for i in range(len(value) - 1))
        return tuple(coerce_relation(item) for item in value)
    return (coerce_relation(value),)


@dataclass(frozen=True)
class RowGroupSummary:
    columns: dict[str, ColumnStats] = field(default_factory=dict)
    relationships: tuple[Relation, ...] = ()

    @staticmethod
    def coerce(
        stats: Any = None,
        relationships: Any = None,
        *,
        columns: Any = None,
        column_stats: Any = None,
        relations: Any = None,
    ) -> "RowGroupSummary":
        source = columns if columns is not None else column_stats
        if source is None:
            source = stats

        col_stats: dict[str, ColumnStats] = {}
        if isinstance(source, RowGroupSummary):
            col_stats = dict(source.columns)
            rels = source.relationships
        else:
            col_stats = _coerce_stats_mapping(source)
            rels = ()

        rel_source = relationships
        if rel_source is None:
            rel_source = relations
        if rel_source is None and isinstance(source, RowGroupSummary):
            rel_source = source.relationships
        parsed_rels = coerce_relations(rel_source)
        return RowGroupSummary(col_stats, parsed_rels)


def _coerce_stats_mapping(source: Any) -> dict[str, ColumnStats]:
    if source is None:
        return {}
    if isinstance(source, Mapping):
        result = {}
        for name, value in source.items():
            result[str(name)] = coerce_column_stats(value, str(name))
        return result
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        # Allow [ColumnStats(...), ...] only if names are available elsewhere;
        # ordinary callers should use a mapping.
        result = {}
        for i, value in enumerate(source):
            if isinstance(value, Mapping) and "name" in value:
                name = str(value["name"])
                result[name] = coerce_column_stats(value, name)
            elif isinstance(value, tuple) and value and isinstance(value[0], str):
                result[value[0]] = coerce_column_stats(value[1:], value[0])
            else:
                raise TypeError("sequence stats must contain named mappings or (name, stats...) tuples")
        return result
    raise TypeError("stats must be a mapping of column name to ColumnStats")
