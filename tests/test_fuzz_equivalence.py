"""Deterministic randomized (seeded) interleave tests.

The exhaustive tests cover every small-table instance for hand-picked
predicates; this module complements them by generating many *different*
random expression shapes (deeply nested AND/OR/NOT, IS NULL, all comparison
operators, cross-side references) over random join trees with random
small tables, and checking original-vs-rewritten bag equivalence. The RNG
seed is fixed so failures are reproducible.
"""

import random
import unittest

from predrewrite.expressions import (
    And,
    Column,
    Compare,
    Expr,
    IsNull,
    Literal,
    Not,
    Or,
)
from predrewrite.plans import (
    Filter,
    InnerJoin,
    LeftOuterJoin,
    Plan,
    Scan,
    Table,
    canonical_bag,
    evaluate_plan,
)
from predrewrite.rewrite import rewrite

DOMAIN = (None, 0, 1, 2, 3)
OPS = ("=", "<>", "<", "<=", ">", ">=")
COLS = ("k", "v")
ALIAS_POOL = ("a", "b", "c", "d")


def _rng_table(rng: random.Random, name: str) -> Table:
    size = rng.randrange(0, 4)
    rows = tuple({col: rng.choice(DOMAIN) for col in COLS} for _ in range(size))
    return Table(name, COLS, rows)


def _rng_plan(rng: random.Random, size: int, alias_pool: list[str]) -> Plan:
    """Build a random join tree, consuming unique aliases from ``alias_pool``."""
    if size == 1:
        alias = alias_pool.pop()
        return Scan(
            alias=alias,
            table=_rng_table(rng, alias.upper()),
            node_id=f"scan_{alias}",
        )
    split = rng.randrange(1, size)
    left = _rng_plan(rng, split, alias_pool)
    right = _rng_plan(rng, size - split, alias_pool)
    on = Compare(
        Column(_leftmost_alias(left), "k"),
        "=",
        Column(_leftmost_alias(right), "k"),
    )
    cls = LeftOuterJoin if rng.random() < 0.7 else InnerJoin
    return cls(
        left=left, right=right, on=on,
        node_id=f"j_{rng.randrange(10**6)}",
    )


def _leftmost_alias(plan: Plan) -> str:
    if isinstance(plan, Scan):
        return plan.alias
    if isinstance(plan, Filter):
        return _leftmost_alias(plan.source)
    return _leftmost_alias(plan.left)


def _all_aliases(plan: Plan) -> tuple[str, ...]:
    if isinstance(plan, Scan):
        return (plan.alias,)
    if isinstance(plan, Filter):
        return _all_aliases(plan.source)
    return _all_aliases(plan.left) + _all_aliases(plan.right)


def _constrained_conjunct(
    rng: random.Random, aliases: tuple[str, ...], depth: int
) -> Expr:
    """A single top-level conjunct: never itself an And, so predicate ids
    stay in one-to-one correspondence with the top-level conjunct list."""
    pick = lambda: Column(rng.choice(aliases), rng.choice(COLS))
    kind = rng.randrange(4) if depth > 0 else rng.randrange(3)
    if kind == 0:
        return Compare(pick(), rng.choice(OPS), Literal(rng.choice(DOMAIN)))
    if kind == 1:
        return Compare(pick(), rng.choice(OPS), pick())
    if kind == 2:
        return IsNull(pick(), negated=rng.choice([False, True]))
    if kind == 3:
        # OR / NOT nestings are fine inside one conjunct.
        return Or(
            [
                _constrained_expr(rng, aliases, depth - 1)
                for _ in range(rng.randrange(2, 4))
            ]
        )
    return Not(_constrained_expr(rng, aliases, depth - 1))


def _constrained_expr(
    rng: random.Random, aliases: tuple[str, ...], depth: int
) -> Expr:
    kind = rng.randrange(5) if depth > 0 else rng.randrange(3)
    pick = lambda: Column(rng.choice(aliases), rng.choice(COLS))
    if kind == 0:
        return Compare(pick(), rng.choice(OPS), Literal(rng.choice(DOMAIN)))
    if kind == 1:
        return Compare(pick(), rng.choice(OPS), pick())
    if kind == 2:
        return IsNull(pick(), negated=rng.choice([False, True]))
    if kind == 3:
        op_cls = rng.choice([And, Or])
        return op_cls(
            [
                _constrained_expr(rng, aliases, depth - 1)
                for _ in range(rng.randrange(2, 4))
            ]
        )
    return Not(_constrained_expr(rng, aliases, depth - 1))


def _node_ids(plan: Plan) -> set[str]:
    if isinstance(plan, Scan):
        return {plan.node_id}
    if isinstance(plan, Filter):
        return {plan.node_id} | _node_ids(plan.source)
    return {plan.node_id} | _node_ids(plan.left) | _node_ids(plan.right)


class SeededFuzzEquivalenceTests(unittest.TestCase):
    def test_many_random_plans_and_predicates(self) -> None:
        rng = random.Random(20261123)
        trials = 250
        checked = 0
        for trial in range(trials):
            size = rng.randrange(2, 5)
            aliases = list(ALIAS_POOL[:size])
            plan = _rng_plan(rng, size, aliases)
            schema_aliases = _all_aliases(plan)
            self.assertEqual(len(set(schema_aliases)), size)

            conjuncts = [
                _constrained_conjunct(rng, schema_aliases, 3)
                for _ in range(rng.randrange(1, 4))
            ]
            pids = tuple(f"t{trial}_p{i}" for i in range(len(conjuncts)))
            filtered = Filter(
                source=plan,
                expr=And(conjuncts),
                pids=pids,
                node_id=f"where_{trial}",
            )

            result = rewrite(filtered)
            before = canonical_bag(evaluate_plan(filtered))
            after = canonical_bag(evaluate_plan(result.plan))
            self.assertEqual(
                before,
                after,
                msg=(
                    f"fuzz trial {trial} mismatch\n"
                    f"original ids: {pids}\n"
                    f"conversions: {result.conversion_events}"
                ),
            )
            # Trace integrity: every input pid appears and its destination
            # is a real node id in the rewritten tree.
            destinations = result.destinations()
            self.assertEqual(set(destinations), set(pids))
            node_ids = _node_ids(result.plan)
            for pid, dest in destinations.items():
                self.assertIn(dest, node_ids)
            checked += 1
        self.assertEqual(checked, trials)

    def test_idempotence_on_random_outputs(self) -> None:
        rng = random.Random(99173)
        for _ in range(60):
            size = rng.randrange(2, 5)
            aliases = list(ALIAS_POOL[:size])
            plan = _rng_plan(rng, size, aliases)
            schema_aliases = _all_aliases(plan)
            conjuncts = [
                _constrained_conjunct(rng, schema_aliases, 2)
                for _ in range(rng.randrange(1, 3))
            ]
            filtered = Filter(
                source=plan, expr=And(conjuncts), pids=(), node_id="where"
            )
            first = rewrite(filtered)
            second = rewrite(first.plan)
            self.assertEqual(
                canonical_bag(evaluate_plan(first.plan)),
                canonical_bag(evaluate_plan(second.plan)),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
