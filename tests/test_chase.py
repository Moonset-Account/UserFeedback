"""Tableau building and chase-rule unit tests."""

import unittest

from relanalysis.chase import (
    chase_tableau,
    interleaved_swaps,
    normalize_substitutions,
)
from relanalysis.model import Schema, make_dependencies, make_subtables
from relanalysis.tableau import build_seeds, distinguished, is_distinguished


def _resolve_once(symbol, subs):
    seen = set()
    while symbol in subs and symbol not in seen:
        seen.add(symbol)
        symbol = subs[symbol]
    return symbol


class TableauTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema.from_columns(["A", "B", "C"])
        self.subs = make_subtables(self.schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])

    def test_seeds_use_distinguished_symbols_on_own_columns(self):
        seeds, meta = build_seeds(self.schema, self.subs)
        self.assertEqual(seeds[0], {"A": "a:A", "B": "a:B", "C": "n:0:C"})
        self.assertEqual(seeds[1], {"A": "n:1:A", "B": "a:B", "C": "a:C"})
        self.assertEqual([m["subtable"] for m in meta], ["R1", "R2"])

    def test_symbol_helpers(self):
        self.assertTrue(is_distinguished(distinguished("A")))
        self.assertFalse(is_distinguished("n:0:A"))


class NormalizeTests(unittest.TestCase):
    def test_chain_resolution(self):
        subs = normalize_substitutions([("b", "a"), ("c", "b")])
        self.assertEqual(subs["b"], "a")
        self.assertEqual(subs["c"], "a")

    def test_distinguished_wins_over_null(self):
        subs = normalize_substitutions([("n:0:C", "a:C")])
        self.assertEqual(subs, {"n:0:C": "a:C"})

    def test_no_op_pairs(self):
        self.assertEqual(normalize_substitutions([("a", "a")]), {})

    def test_conflicting_equalities_merge_transitively(self):
        subs = normalize_substitutions([("n:0:C", "n:1:C"), ("n:2:C", "n:0:C")])
        representatives = {
            symbol: _resolve_once(symbol, subs) for symbol in ("n:0:C", "n:1:C", "n:2:C")
        }
        self.assertEqual(len(set(representatives.values())), 1)


class ChaseTests(unittest.TestCase):
    def chase(self, schema_cols, subtables, deps, **kwargs):
        schema = Schema.from_columns(schema_cols)
        subs = make_subtables(schema, subtables)
        dependencies = make_dependencies(schema, deps)
        seeds, _ = build_seeds(schema, subs)
        return schema, dependencies, chase_tableau(
            schema, seeds, [s.name for s in subs], dependencies, **kwargs
        )

    def test_fd_closes_and_yields_witness(self):
        schema, deps, run = self.chase(
            ["A", "B", "C"], [("R1", ["A", "B"]), ("R2", ["B", "C"])], [("FD", ("B",), ("C",))]
        )
        self.assertIsNotNone(run.witness_row_id)
        self.assertTrue(run.steps)
        self.assertEqual(run.steps[0]["kind"], "fd-merge")
        self.assertEqual(run.steps[0]["affected_rows"], [0])

    def test_no_dependency_never_closes(self):
        schema, deps, run = self.chase(
            ["A", "B", "C"], [("R1", ["A", "B"]), ("R2", ["B", "C"])], []
        )
        self.assertIsNone(run.witness_row_id)
        self.assertTrue(run.closed)

    def test_mvd_swap_adds_row(self):
        schema, deps, run = self.chase(
            ["A", "B", "C", "D"],
            [("R1", ["A", "B"]), ("R2", ["A", "C", "D"])],
            [("MVD", ("A",), ("B",))],
        )
        self.assertIsNotNone(run.witness_row_id)
        self.assertEqual(run.steps[-1]["kind"], "mvd-swap")
        self.assertIn(run.witness_row_id, run.rows)

    def test_stop_at_witness_can_be_disabled(self):
        schema_cols = ["A", "B", "C"]
        schema = Schema.from_columns(schema_cols)
        subs = make_subtables(schema, [("R1", ["A", "B"]), ("R2", ["B", "C"])])
        deps = make_dependencies(schema, [("FD", ("B",), ("C",))])
        seeds, _ = build_seeds(schema, subs)
        run = chase_tableau(
            schema, seeds, [s.name for s in subs], deps, stop_at_witness=False
        )
        self.assertIsNotNone(run.witness_row_id)
        self.assertTrue(run.closed)

    def test_witness_is_all_distinguished(self):
        schema, deps, run = self.chase(
            ["A", "B", "C"], [("R1", ["A", "B"]), ("R2", ["B", "C"])], [("FD", ("B",), ("C",))]
        )
        witness = run.witness_row()
        self.assertEqual(witness, {"A": "a:A", "B": "a:B", "C": "a:C"})

    def test_meta_records_mvd_lineage(self):
        schema, deps, run = self.chase(
            ["A", "B", "C", "D"],
            [("R1", ["A", "B"]), ("R2", ["A", "C", "D"])],
            [("MVD", ("A",), ("B",))],
        )
        mvd_rows = [rid for rid, m in run.meta.items() if m["origin"] == "mvd"]
        self.assertTrue(mvd_rows)
        for rid in mvd_rows:
            self.assertIn(run.meta[rid]["created_step"], {s["seq"] for s in run.steps})


class InterleavingDetectionTests(unittest.TestCase):
    def test_finds_mvd_fd_mvd_pattern(self):
        schema = Schema.from_columns(["A", "B", "C", "D"])
        subs = make_subtables(
            schema, [("R1", ["A", "B"]), ("R2", ["A", "C"]), ("R3", ["B", "D"])]
        )
        deps = make_dependencies(
            schema, [("FD", ("C",), ("A",)), ("MVD", ("A",), ("B",)), ("MVD", ("B",), ("C",))]
        )
        seeds, _ = build_seeds(schema, subs)
        run = chase_tableau(schema, seeds, [s.name for s in subs], deps)
        self.assertIsNotNone(run.witness_row_id)
        observations = interleaved_swaps(run.meta, run.steps)
        self.assertTrue(observations)
        for obs in observations:
            self.assertEqual(obs["kind"], "mvd-after-fd-merge-of-mvd-row")
            for parent in obs["merged_parents"]:
                self.assertLess(parent["created_by_step"], obs["step"])
                for merge in parent["fd_merge_steps"]:
                    self.assertGreater(merge, parent["created_by_step"])
                    self.assertLess(merge, obs["step"])

    def test_simple_mvd_case_has_no_interleaving(self):
        schema = Schema.from_columns(["A", "B", "C", "D"])
        subs = make_subtables(schema, [("R1", ["A", "B"]), ("R2", ["A", "C", "D"])])
        deps = make_dependencies(schema, [("MVD", ("A",), ("B",))])
        seeds, _ = build_seeds(schema, subs)
        run = chase_tableau(schema, seeds, [s.name for s in subs], deps)
        self.assertEqual(interleaved_swaps(run.meta, run.steps), [])


if __name__ == "__main__":
    unittest.main()
