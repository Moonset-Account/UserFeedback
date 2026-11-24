"""Linear-expression normalization and conjunction cubes used by the solver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .expressions import Arithmetic, Column, Expr, Literal


class UnsupportedExpressionError(NotImplementedError):
    """Raised when an expression is outside the difference-constraint subset."""


ZERO_VAR = "\x00row_group_zero"


@dataclass(frozen=True)
class Linear:
    """An integer linear form: ``sum(coeffs[name] * name) + const``."""

    coeffs: dict[str, int] = field(default_factory=dict)
    const: int = 0

    def normalized(self) -> "Linear":
        return Linear({k: v for k, v in self.coeffs.items() if v != 0}, self.const)

    def __add__(self, other: "Linear") -> "Linear":
        coeffs = dict(self.coeffs)
        for name, value in other.coeffs.items():
            coeffs[name] = coeffs.get(name, 0) + value
        return Linear(coeffs, self.const + other.const).normalized()

    def __sub__(self, other: "Linear") -> "Linear":
        coeffs = dict(self.coeffs)
        for name, value in other.coeffs.items():
            coeffs[name] = coeffs.get(name, 0) - value
        return Linear(coeffs, self.const - other.const).normalized()

    def negate(self) -> "Linear":
        return Linear(
            {name: -value for name, value in self.coeffs.items()},
            -self.const,
        ).normalized()


def as_linear(expr: Expr) -> Linear:
    if isinstance(expr, Column):
        return Linear({expr.name: 1}, 0)
    if isinstance(expr, Literal):
        if isinstance(expr.value, bool):
            return Linear({}, int(expr.value))
        if isinstance(expr.value, int):
            return Linear({}, expr.value)
        if isinstance(expr.value, float) and expr.value.is_integer():
            return Linear({}, int(expr.value))
        raise UnsupportedExpressionError(f"non-integer literal {expr.value!r}")
    if isinstance(expr, Arithmetic):
        left = as_linear(expr.left)
        right = as_linear(expr.right)
        return left + right if expr.op == "+" else left - right
    raise UnsupportedExpressionError(
        f"expression of type {type(expr).__name__} is not an integer linear form"
    )


def contains_null_literal(expr: Expr) -> bool:
    stack = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, Literal) and node.value is None:
            return True
        if hasattr(node, "left") and hasattr(node, "right"):
            stack.extend((node.left, node.right))
        elif hasattr(node, "operand"):
            stack.append(node.operand)
    return False


@dataclass(frozen=True)
class Diff:
    """Difference bound: ``u - v <= bound``."""

    u: str
    v: str
    bound: int


@dataclass(frozen=True)
class Neq:
    """Difference inequality: ``u - v != value``."""

    u: str
    v: str
    value: int


@dataclass(frozen=True)
class Cube:
    """A conjunction branch in disjunctive normal form."""

    nulls: tuple[tuple[str, bool], ...] = ()
    diffs: tuple[Diff, ...] = ()
    neqs: tuple[Neq, ...] = ()
    labels: tuple[str, ...] = ()
    conflict: str | None = None

    @staticmethod
    def empty(label: str | None = None) -> "Cube":
        return Cube(labels=(label,) if label else ())

    @staticmethod
    def impossible(reason: str, label: str | None = None) -> "Cube":
        return Cube(conflict=reason, labels=(label,) if label else ())

    @staticmethod
    def null_state(name: str, is_null: bool, label: str | None = None) -> "Cube":
        if label is None:
            label = f"{name} IS {'NULL' if is_null else 'NOT NULL'}"
        return Cube(nulls=((name, is_null),), labels=(label,))

    @staticmethod
    def non_null(names: list[str] | set[str] | tuple[str, ...]) -> "Cube":
        ordered = tuple(sorted(names))
        return Cube(nulls=tuple((name, False) for name in ordered))

    @staticmethod
    def from_diffs(
        diffs: list[Diff] | tuple[Diff, ...],
        label: str,
        neqs: list[Neq] | tuple[Neq, ...] = (),
    ) -> "Cube":
        return Cube(diffs=tuple(diffs), neqs=tuple(neqs), labels=(label,))


def merge_cubes(a: Cube, b: Cube) -> Cube:
    labels: list[str] = []
    for label in (*a.labels, *b.labels):
        if label and label not in labels:
            labels.append(label)

    if a.conflict is not None or b.conflict is not None:
        reasons = [x for x in (a.conflict, b.conflict) if x]
        return Cube(
            nulls=_merge_null_tuples(a.nulls, b.nulls)[1],
            diffs=a.diffs + b.diffs,
            neqs=a.neqs + b.neqs,
            labels=tuple(labels),
            conflict="; ".join(reasons) or "impossible conjunction",
        )

    null_map: dict[str, bool] = {}
    for name, state in a.nulls:
        null_map[name] = state
    for name, state in b.nulls:
        old = null_map.get(name)
        if old is not None and old != state:
            reason = (
                f"空值状态冲突：{name} 不能同时要求 IS NULL 和 IS NOT NULL"
            )
            return Cube(
                nulls=tuple(sorted(null_map.items())),
                diffs=a.diffs + b.diffs,
                neqs=a.neqs + b.neqs,
                labels=tuple(labels),
                conflict=reason,
            )
        null_map[name] = state

    return Cube(
        nulls=tuple(sorted(null_map.items())),
        diffs=a.diffs + b.diffs,
        neqs=a.neqs + b.neqs,
        labels=tuple(labels),
    )


def _merge_null_tuples(
    a: tuple[tuple[str, bool], ...], b: tuple[tuple[str, bool], ...]
) -> tuple[bool, tuple[tuple[str, bool], ...]]:
    null_map: dict[str, bool] = {}
    conflict = False
    for name, state in (*a, *b):
        if name in null_map and null_map[name] != state:
            conflict = True
        null_map[name] = state
    return conflict, tuple(sorted(null_map.items()))


def _floor_div(a: int, b: int) -> int:
    return a // b


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


def le_zero_cube(form: Linear, label: str) -> Cube:
    """Create cube for ``sum(coeff*x) + const <= 0``."""
    form = form.normalized()
    coeffs = form.coeffs
    k = form.const

    if not coeffs:
        if k <= 0:
            return Cube.empty(label)
        return Cube.impossible(f"常量条件不成立：{label}", label)

    if len(coeffs) == 1:
        name, coefficient = next(iter(coeffs.items()))
        if coefficient > 0:
            bound = _floor_div(-k, coefficient)
            return Cube.from_diffs([Diff(name, ZERO_VAR, bound)], label)
        magnitude = -coefficient
        lower = _ceil_div(k, magnitude)
        return Cube.from_diffs([Diff(ZERO_VAR, name, -lower)], label)

    if len(coeffs) == 2:
        (u, cu), (v, cv) = tuple(coeffs.items())
        if cu > 0 and cv < 0 and cu == -cv:
            bound = _floor_div(-k, cu)
            return Cube.from_diffs([Diff(u, v, bound)], label)
        if cv > 0 and cu < 0 and cv == -cu:
            bound = _floor_div(-k, cv)
            return Cube.from_diffs([Diff(v, u, bound)], label)

    raise UnsupportedExpressionError(
        "only one-column integer bounds or equal-coefficient column differences are supported"
    )


def neq_zero_cube(form: Linear, label: str) -> Cube:
    """Create cube for ``sum(coeff*x) + const != 0``."""
    form = form.normalized()
    coeffs = form.coeffs
    k = form.const

    if not coeffs:
        if k != 0:
            return Cube.empty(label)
        return Cube.impossible(f"常量条件不成立：{label}", label)

    if len(coeffs) == 1:
        name, coefficient = next(iter(coeffs.items()))
        # coefficient*x + k = 0 has an integer solution only when divisible.
        if (-k) % coefficient != 0:
            return Cube.empty(label)
        value = (-k) // coefficient
        # Normalize to x - ZERO != value, regardless of coefficient sign because
        # divisibility above gives the unique integer root.
        return Cube(neqs=(Neq(name, ZERO_VAR, value),), labels=(label,))

    if len(coeffs) == 2:
        (u, cu), (v, cv) = tuple(coeffs.items())
        if cu > 0 and cv < 0 and cu == -cv:
            magnitude = cu
            if (-k) % magnitude != 0:
                return Cube.empty(label)
            value = (-k) // magnitude
            return Cube(neqs=(Neq(u, v, value),), labels=(label,))
        if cv > 0 and cu < 0 and cv == -cu:
            magnitude = cv
            if (-k) % magnitude != 0:
                return Cube.empty(label)
            value = (-k) // magnitude
            return Cube(neqs=(Neq(v, u, value),), labels=(label,))

    raise UnsupportedExpressionError(
        "unsupported != linear expression for difference solver"
    )


def comparison_cube(op: str, left: Expr, right: Expr, label: str) -> Cube:
    """Build the non-NULL integer constraint cube for a comparison."""
    left_linear = as_linear(left)
    right_linear = as_linear(right)
    form = left_linear - right_linear  # left - right

    if op == "<=":
        return le_zero_cube(form, label)
    if op == "<":
        # Integer values: form < 0  <=>  form + 1 <= 0.
        return le_zero_cube(Linear(form.coeffs, form.const + 1), label)
    if op == ">=":
        return le_zero_cube(form.negate(), label)
    if op == ">":
        # form > 0 <=> -form + 1 <= 0.
        negated = form.negate()
        return le_zero_cube(Linear(negated.coeffs, negated.const + 1), label)
    if op == "=":
        first = le_zero_cube(form, label)
        second = le_zero_cube(form.negate(), label)
        return merge_cubes(first, second)
    if op == "!=":
        return neq_zero_cube(form, label)
    raise UnsupportedExpressionError(f"unsupported operator {op!r}")
