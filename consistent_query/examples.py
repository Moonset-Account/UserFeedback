"""Worked examples, including the two-conflicting-key-groups construction.

Run ``python3 -m consistent_query`` to print the engine's verdicts and the
independent oracle's verdicts side by side.
"""

from __future__ import annotations

from .engine import AnalysisReport, Limits, delete_row, keep_row
from .model import Denial, ForeignKey, Problem, Query, Row, Table


def conflicting_groups_problem() -> Problem:
    """Two key groups, each with a conflict, and a query joining across them.

    Group ``L`` holds ``l1`` and ``l2``, which share ``k = 1``; group ``R``
    holds ``r1`` and ``r2``, which also share ``k = 1``.  With unit costs the
    minimum-cost repairs delete exactly one row from each group, so there are
    four optimal repairs and **no row survives in all of them**.

    Yet the query

        SELECT L.label, R.code FROM L, R WHERE L.k = R.k

    derives ``('L', 'C')`` in every optimal repair: whichever ``l`` and
    ``r`` survive, their joined row projects to the same tuple.  Each repair
    uses a different derivation, so certainty cannot be established from a
    single derivation -- it is a union over repairs.
    """

    left = Table(
        name="L",
        columns=("k", "label", "v"),
        keys=(("k",),),
        rows=(
            Row("l1", {"k": 1, "label": "L", "v": "x"}),
            Row("l2", {"k": 1, "label": "L", "v": "y"}),
        ),
    )
    right = Table(
        name="R",
        columns=("k", "code", "v"),
        keys=(("k",),),
        rows=(
            Row("r1", {"k": 1, "code": "C", "v": "x"}),
            Row("r2", {"k": 1, "code": "C", "v": "y"}),
        ),
    )
    return Problem(tables=(left, right))


def conflicting_groups_certain_query() -> Query:
    return Query(
        tables=("L", "R"),
        equalities=((("L", "k"), ("R", "k")),),
        projection=(("L", "label"), ("R", "code")),
    )


def conflicting_groups_pair_query() -> Query:
    """Same join, but projecting the distinguishing ``v`` columns."""

    return Query(
        tables=("L", "R"),
        equalities=((("L", "k"), ("R", "k")),),
        projection=(("L", "v"), ("R", "v")),
    )


def three_status_problem() -> Problem:
    """Asymmetric costs plus a free choice: certain, contingent and absent.

    ``P`` has a key conflict between ``p1`` (cheap) and ``p2`` (expensive), so
    the unique optimum keeps ``p2`` and deletes ``p1``.  ``Q`` has two equally
    priced conflicting rows, so either may survive.
    """

    p = Table(
        name="P",
        columns=("k", "v"),
        keys=(("k",),),
        rows=(
            Row("p1", {"k": 1, "v": "a"}, cost=1),
            Row("p2", {"k": 1, "v": "b"}, cost=3),
        ),
    )
    q = Table(
        name="Q",
        columns=("k", "w"),
        keys=(("k",),),
        rows=(
            Row("q1", {"k": 1, "w": "u"}),
            Row("q2", {"k": 1, "w": "v"}),
        ),
    )
    return Problem(tables=(p, q))


def three_status_query() -> Query:
    return Query(
        tables=("P", "Q"),
        equalities=((("P", "k"), ("Q", "k")),),
        projection=(("P", "v"), ("Q", "w")),
    )


def referential_problem() -> Problem:
    """Mandatory and optional foreign keys plus an explicit denial rule.

    ``orders.customer`` is a mandatory foreign key to ``customers.id``;
    ``orders.referrer`` is optional and follows SQL ``MATCH SIMPLE``.
    A denial rule forbids two orders of the same ``region`` from coexisting,
    modelling an exclusivity rule rather than a key.
    """

    customers = Table(
        name="customers",
        columns=("id", "name"),
        keys=(("id",),),
        rows=(
            Row("c1", {"id": 1, "name": "ada"}),
            Row("c2", {"id": 1, "name": "ada-dup"}),
            Row("c3", {"id": 2, "name": "grace"}),
        ),
    )
    orders = Table(
        name="orders",
        columns=("oid", "customer", "referrer", "region"),
        keys=(("oid",),),
        rows=(
            Row("o1", {"oid": 10, "customer": 1, "referrer": 2, "region": "eu"}),
            Row("o2", {"oid": 11, "customer": 1, "referrer": None, "region": "eu"}),
            Row("o3", {"oid": 12, "customer": 9, "referrer": 1, "region": "us"}),
            Row("o4", {"oid": 13, "customer": 2, "referrer": 2, "region": "ap"}),
        ),
    )
    foreign_keys = (
        ForeignKey("orders", ("customer",), "customers", ("id",), mandatory=True),
        ForeignKey("orders", ("referrer",), "customers", ("id",), mandatory=False),
    )
    denials = (
        Denial(
            left_table="orders",
            right_table="orders",
            left_columns=("region",),
            right_columns=("region",),
        ),
    )
    return Problem(tables=(customers, orders), foreign_keys=foreign_keys, denials=denials)


def referential_query() -> Query:
    return Query(
        tables=("orders", "customers"),
        equalities=((("orders", "customer"), ("customers", "id")),),
        projection=(("orders", "oid"), ("customers", "name")),
    )


__all__ = [
    "conflicting_groups_certain_query",
    "conflicting_groups_pair_query",
    "conflicting_groups_problem",
    "referential_problem",
    "referential_query",
    "three_status_problem",
    "three_status_query",
]
