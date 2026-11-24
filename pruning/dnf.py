"""Three-valued logic compilation into truth / false / not-true / not-false DNFs."""

from __future__ import annotations

from dataclasses import dataclass

from .expressions import (
    And,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
    referenced_columns,
)
from .linear import (
    Cube,
    UnsupportedExpressionError,
    comparison_cube,
    contains_null_literal,
    merge_cubes,
)
from .render import render_expr


@dataclass(frozen=True)
class FourDNF:
    # Each field is a DNF represented as a tuple of conjunction cubes.
    true: tuple[Cube, ...]
    false: tuple[Cube, ...]
    not_true: tuple[Cube, ...]
    not_false: tuple[Cube, ...]


def _and_dnf(a: tuple[Cube, ...], b: tuple[Cube, ...]) -> tuple[Cube, ...]:
    result: list[Cube] = []
    for left in a:
        for right in b:
            result.append(merge_cubes(left, right))
    return tuple(result)


def _or_dnf(a: tuple[Cube, ...], b: tuple[Cube, ...]) -> tuple[Cube, ...]:
    return a + b


def _null_choice_cubes(columns: set[str]) -> tuple[Cube, ...]:
    return tuple(Cube.null_state(name, True) for name in sorted(columns))


def _scalar_null_test(operand: Expr, negated: bool) -> FourDNF:
    """DNF for NULL tests over scalar/integer expressions."""
    label = render_expr(IsNull(operand, negated))

    if contains_null_literal(operand):
        # A scalar NULL literal is always NULL.
        is_null_branches = (Cube.empty(label),)
        is_not_null_branches: tuple[Cube, ...] = ()
    else:
        columns = referenced_columns(operand)
        if not columns:
            # A non-NULL constant scalar.
            is_null_branches = ()
            is_not_null_branches = (Cube.empty(label),)
        else:
            is_null_branches = _null_choice_cubes(columns)
            non_null = Cube.non_null(columns)
            is_not_null_branches = (
                Cube(
                    nulls=non_null.nulls,
                    labels=(label,),
                ),
            )

    if not negated:
        true_branches = is_null_branches
        false_branches = is_not_null_branches
    else:
        true_branches = is_not_null_branches
        false_branches = is_null_branches

    # IS [NOT] NULL always returns TRUE or FALSE, never UNKNOWN.
    return FourDNF(
        true=true_branches,
        false=false_branches,
        not_true=false_branches,
        not_false=true_branches,
    )


def _boolean_null_test(node: IsNull, inner: FourDNF) -> FourDNF:
    """General ``boolean_expr IS [NOT] NULL`` using the UNKNOWN region."""
    unknown = _and_dnf(inner.not_true, inner.not_false)
    true_or_false = _or_dnf(inner.true, inner.false)

    if not node.negated:
        true_branches = unknown
        false_branches = true_or_false
    else:
        true_branches = true_or_false
        false_branches = unknown

    return FourDNF(
        true=true_branches,
        false=false_branches,
        not_true=false_branches,
        not_false=true_branches,
    )


def _cross_mode_nodes(left: Expr, left_mode: str, right: Expr, right_mode: str):
    for left_cube in iter_dnf(left, left_mode):
        if left_cube.conflict is not None:
            # A conflicting conjunction cannot become feasible by adding more
            # conditions; one representative is enough for sound enumeration.
            yield left_cube
            continue
        produced = False
        for right_cube in iter_dnf(right, right_mode):
            produced = True
            yield merge_cubes(left_cube, right_cube)
        if not produced:
            # Right DNF is empty: conjunction is empty, so nothing to yield.
            pass


def _same_mode_cross(left: Expr, right: Expr, mode: str):
    yield from _cross_mode_nodes(left, mode, right, mode)


def iter_dnf(expr: Expr, mode: str):
    """Lazily yield cubes for one of modes ``T``, ``F``, ``N``, or ``P``.

    T = SQL TRUE, F = FALSE, N = not TRUE (FALSE or UNKNOWN), and
    P = not FALSE (TRUE or UNKNOWN).
    """
    mode = mode.upper()
    if mode not in ("T", "F", "N", "P"):
        raise ValueError(f"unknown DNF mode {mode!r}")

    if isinstance(expr, Literal):
        if expr.value is True:
            cube = Cube.empty("TRUE")
            yield from (cube,) if mode in ("T", "P") else ()
            return
        if expr.value is False:
            cube = Cube.empty("FALSE")
            yield from (cube,) if mode in ("F", "N") else ()
            return
        cube = Cube.empty("NULL yields UNKNOWN")
        yield from (cube,) if mode in ("N", "P") else ()
        return

    if isinstance(expr, Compare):
        all_sets = build_dnf(expr)
        yield from getattr(all_sets, {"T": "true", "F": "false", "N": "not_true", "P": "not_false"}[mode])
        return

    if isinstance(expr, IsNull):
        # Scalar NULL tests can reuse the small eager implementation.
        if isinstance(expr.operand, (Column, Literal)) or not isinstance(
            expr.operand, (Compare, IsNull, Not, And, Or)
        ):
            all_sets = _scalar_null_test(expr.operand, expr.negated)
            yield from getattr(all_sets, {"T": "true", "F": "false", "N": "not_true", "P": "not_false"}[mode])
            return

        inner = expr.operand
        if not expr.negated:
            if mode == "T":
                yield from _cross_mode_nodes(inner, "N", inner, "P")
            elif mode == "F":
                yield from iter_dnf(inner, "T")
                yield from iter_dnf(inner, "F")
            elif mode == "N":
                yield from iter_dnf(inner, "T")
                yield from iter_dnf(inner, "F")
            else:  # P is the same as T for a two-valued IS NULL predicate.
                yield from _cross_mode_nodes(inner, "N", inner, "P")
        else:
            if mode == "T":
                yield from iter_dnf(inner, "T")
                yield from iter_dnf(inner, "F")
            elif mode in ("F", "N"):
                yield from _cross_mode_nodes(inner, "N", inner, "P")
            else:
                yield from iter_dnf(inner, "T")
                yield from iter_dnf(inner, "F")
        return

    if isinstance(expr, Not):
        child_mode = {"T": "F", "F": "T", "N": "P", "P": "N"}[mode]
        yield from iter_dnf(expr.operand, child_mode)
        return

    if isinstance(expr, And):
        if mode == "T":
            yield from _same_mode_cross(expr.left, expr.right, "T")
        elif mode == "F":
            yield from iter_dnf(expr.left, "F")
            yield from iter_dnf(expr.right, "F")
        elif mode == "N":
            yield from iter_dnf(expr.left, "N")
            yield from iter_dnf(expr.right, "N")
        else:
            yield from _same_mode_cross(expr.left, expr.right, "P")
        return

    if isinstance(expr, Or):
        if mode == "T":
            yield from iter_dnf(expr.left, "T")
            yield from iter_dnf(expr.right, "T")
        elif mode == "F":
            yield from _same_mode_cross(expr.left, expr.right, "F")
        elif mode == "N":
            yield from _same_mode_cross(expr.left, expr.right, "N")
        else:
            yield from iter_dnf(expr.left, "P")
            yield from iter_dnf(expr.right, "P")
        return

    raise UnsupportedExpressionError(f"unsupported expression node: {type(expr).__name__}")


def build_dnf(expr: Expr) -> FourDNF:
    if isinstance(expr, Literal):
        if expr.value is True:
            empty = (Cube.empty("TRUE"),)
            none: tuple[Cube, ...] = ()
            return FourDNF(empty, none, none, empty)
        if expr.value is False:
            empty = (Cube.empty("FALSE"),)
            none = ()
            return FourDNF(none, empty, empty, none)
        if expr.value is None:
            # A bare SQL NULL boolean expression is UNKNOWN.
            empty = (Cube.empty("NULL yields UNKNOWN"),)
            none = ()
            return FourDNF(none, none, empty, empty)
        raise UnsupportedExpressionError(f"integer literal {expr.value!r} is not a boolean predicate")

    if isinstance(expr, Compare):
        label = render_expr(expr)

        # Any comparison with a NULL literal is UNKNOWN under SQL rules.
        if contains_null_literal(expr):
            unknown = (Cube.empty(f"{label} is UNKNOWN because an operand is NULL"),)
            return FourDNF((), (), unknown, unknown)

        columns = referenced_columns(expr)
        guards = Cube.non_null(columns)

        true_condition = comparison_cube(expr.op, expr.left, expr.right, label)
        true_cube = merge_cubes(guards, true_condition)

        negated_op = {
            "=": "!=",
            "!=": "=",
            "<": ">=",
            "<=": ">",
            ">": "<=",
            ">=": "<",
        }[expr.op]
        false_label = f"NOT ({label})"
        false_condition = comparison_cube(negated_op, expr.left, expr.right, false_label)
        false_cube = merge_cubes(guards, false_condition)

        null_branches = _null_choice_cubes(columns)
        # NOT TRUE = some operand is NULL OR comparison is false.
        not_true = null_branches + (false_cube,)
        # NOT FALSE = some operand is NULL OR comparison is true.
        not_false = null_branches + (true_cube,)

        return FourDNF(
            true=(true_cube,),
            false=(false_cube,),
            not_true=not_true,
            not_false=not_false,
        )

    if isinstance(expr, IsNull):
        if isinstance(expr.operand, (Column, Literal)) or not isinstance(
            expr.operand, (Compare, IsNull, Not, And, Or)
        ):
            return _scalar_null_test(expr.operand, expr.negated)
        inner = build_dnf(expr.operand)
        return _boolean_null_test(expr, inner)

    if isinstance(expr, Not):
        child = build_dnf(expr.operand)
        return FourDNF(
            true=child.false,
            false=child.true,
            not_true=child.not_false,
            not_false=child.not_true,
        )

    if isinstance(expr, And):
        left = build_dnf(expr.left)
        right = build_dnf(expr.right)
        return FourDNF(
            true=_and_dnf(left.true, right.true),
            false=_or_dnf(left.false, right.false),
            not_true=_or_dnf(left.not_true, right.not_true),
            not_false=_and_dnf(left.not_false, right.not_false),
        )

    if isinstance(expr, Or):
        left = build_dnf(expr.left)
        right = build_dnf(expr.right)
        return FourDNF(
            true=_or_dnf(left.true, right.true),
            false=_and_dnf(left.false, right.false),
            not_true=_and_dnf(left.not_true, right.not_true),
            not_false=_or_dnf(left.not_false, right.not_false),
        )

    if isinstance(expr, Column):
        raise UnsupportedExpressionError(
            f"bare integer column {expr.name!r} cannot be used as a boolean expression"
        )

    raise UnsupportedExpressionError(f"unsupported expression node: {type(expr).__name__}")
