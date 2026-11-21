"""Unit tests for :mod:`incremental_reach`.

Run from the workspace root::

    python3 -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import random
import unittest

from incremental_reach import Delta, IncrementalReachability
from tests.reference import full_reachable


class _Instrumented(IncrementalReachability):
    """Records which nodes had their outgoing edges expanded."""

    def __init__(self, *args, **kwargs):
        self.expanded: list = []
        super().__init__(*args, **kwargs)

    def _out_adj(self, node):
        self.expanded.append(node)
        return super()._out_adj(node)


class BasicConstructionTests(unittest.TestCase):
    def test_no_seeds_no_edges(self):
        op = IncrementalReachability()
        self.assertEqual(op.reachable, frozenset())

    def test_seeds_are_reachable_without_edges(self):
        op = IncrementalReachability(seeds={"a", "b"})
        self.assertEqual(op.reachable, frozenset({"a", "b"}))

    def test_chain(self):
        op = IncrementalReachability(seeds={1}, edges=[(1, 2), (2, 3)])
        self.assertEqual(op.reachable, frozenset({1, 2, 3}))

    def test_duplicate_edges_are_idempotent(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a"), ("s", "a")])
        self.assertEqual(op.edges, frozenset({("s", "a")}))
        self.assertEqual(op.reachable, frozenset({"s", "a"}))

    def test_self_loop(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "s"), ("s", "a")])
        self.assertEqual(op.reachable, frozenset({"s", "a"}))

    def test_bad_edge_rejected(self):
        with self.assertRaises(ValueError):
            IncrementalReachability(edges=[("only-one",)])

    def test_seed_that_is_also_edge_target(self):
        op = IncrementalReachability(seeds={"b"}, edges=[("a", "b"), ("b", "c")])
        # "a" is not reachable, only "b" and downstream.
        self.assertEqual(op.reachable, frozenset({"b", "c"}))


class DeltaBasicsTests(unittest.TestCase):
    def test_disjoint_added_removed(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        d = op.apply(edges_add=[("a", "b")])
        self.assertTrue(d.added.isdisjoint(d.removed))
        self.assertEqual(d.added, frozenset({"b"}))

    def test_empty_batch_returns_empty_delta(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        d = op.apply()
        self.assertTrue(d.is_empty)
        self.assertFalse(bool(d))
        self.assertEqual(d.added, frozenset())

    def test_remove_absent_edge_is_noop(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        d = op.apply(edges_remove=[("x", "y")])
        self.assertTrue(d.is_empty)
        self.assertEqual(op.reachable, frozenset({"s", "a"}))

    def test_add_duplicate_edge_is_noop(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        d = op.apply(edges_add=[("s", "a")])
        self.assertTrue(d.is_empty)

    def test_delta_replay_reconstructs_state(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        replay = set(op.reachable)
        d = op.apply(edges_add=[("a", "b")], edges_remove=[("s", "a")])
        replay = d.replay_onto(replay)
        self.assertEqual(replay, set(op.reachable))

    def test_replacement_cycle_entry_same_batch_no_flicker(self):
        # s->a is the only external entry into the a<->b cycle.  One atomic
        # batch removes it and adds a fresh entry s->b: the cycle must never
        # appear as removed.
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "a")]
        )
        d = op.apply(edges_remove=[("s", "a")], edges_add=[("s", "b")])
        self.assertEqual(d.removed, frozenset())
        self.assertEqual(d.added, frozenset())
        self.assertEqual(op.reachable, frozenset({"s", "a", "b"}))

    def test_separate_batches_do_flicker_for_same_edit(self):
        # Counterpoint: without batching the same rewiring is two deltas and
        # the intermediate state reports the retraction.
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "a")]
        )
        d1 = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d1.removed, frozenset({"a", "b"}))
        d2 = op.apply(edges_add=[("s", "b")])
        self.assertEqual(d2.added, frozenset({"b", "a"}))
        self.assertEqual(set(op.reachable), {"s", "a", "b"})


class FlickerTests(unittest.TestCase):
    """A replacement path in the same batch must produce no disconnect."""

    def test_replacement_path_same_batch_no_flicker(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a"), ("a", "b")])
        d = op.apply(edges_remove=[("s", "a")], edges_add=[("s", "x"), ("x", "b")])
        # "b" is still reachable -> not reported removed, not reported added.
        self.assertNotIn("b", d.removed)
        self.assertNotIn("b", d.added)
        self.assertEqual(d.added, frozenset({"x"}))
        self.assertEqual(d.removed, frozenset({"a"}))

    def test_replace_seed_same_batch(self):
        op = IncrementalReachability(seeds={"s1"}, edges=[("s1", "a")])
        d = op.apply(seeds_remove=["s1"], seeds_add=["s2"], edges_add=[("s2", "a")])
        # Only the old root itself disappears; "a" is re-supported by s2->a.
        self.assertEqual(d.removed, frozenset({"s1"}))
        self.assertEqual(d.added, frozenset({"s2"}))
        self.assertEqual(op.reachable, frozenset({"s2", "a"}))

    def test_duplicate_in_both_collections_keeps_state(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        d = op.apply(seeds_remove=["s"], seeds_add=["s"])
        self.assertTrue(d.is_empty)
        self.assertEqual(op.reachable, frozenset({"s", "a"}))

    def test_edge_in_both_collections_keeps_state(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a")])
        d = op.apply(edges_remove=[("s", "a")], edges_add=[("s", "a")])
        self.assertTrue(d.is_empty)
        self.assertEqual(op.reachable, frozenset({"s", "a"}))

    def test_remove_then_add_absent_edge_still_adds(self):
        # Batch was [remove (0,2), add (0,2)] but (0,2) did not exist yet.
        # Removing an absent edge is a no-op, so the add must still apply.
        op = IncrementalReachability(seeds={0})
        d = op.apply(edges_remove=[(0, 2)], edges_add=[(0, 2)])
        self.assertEqual(d.added, frozenset({2}))
        self.assertEqual(op.reachable, frozenset({0, 2}))


class CycleTests(unittest.TestCase):
    def test_remove_only_external_entry_retracts_whole_cycle(self):
        # s -> a -> b -> c -> a  (a,b,c form a cycle whose only external entry
        # is s->a; internally every node still has a predecessor).
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "c"), ("c", "a")]
        )
        self.assertEqual(op.reachable, frozenset({"s", "a", "b", "c"}))
        d = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d.removed, frozenset({"a", "b", "c"}))
        self.assertEqual(d.added, frozenset())
        self.assertEqual(op.reachable, frozenset({"s"}))
        # Predecessors truly still exist inside the cycle.
        self.assertEqual(op.predecessors("a"), frozenset({"c"}))

    def test_remove_one_of_two_entries_keeps_cycle(self):
        op = IncrementalReachability(
            seeds={"s"},
            edges=[("s", "a"), ("s", "x"), ("x", "b"),
                   ("a", "b"), ("b", "c"), ("c", "a")],
        )
        d = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d.removed, frozenset())
        self.assertEqual(op.reachable, frozenset({"s", "a", "b", "c", "x"}))

    def test_second_seed_inside_cycle_keeps_cycle(self):
        # seed2 = b is a root inside the cycle; removing s->a leaves the whole
        # cycle supported, because b is itself a seed.
        op = IncrementalReachability(
            seeds={"s", "b"},
            edges=[("s", "a"), ("a", "b"), ("b", "c"), ("c", "a")],
        )
        d = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d.removed, frozenset())
        self.assertEqual(op.reachable, frozenset({"s", "b", "a", "c"}))

    def test_second_seed_feeds_cycle_from_outside(self):
        op = IncrementalReachability(
            seeds={"s", "t"},
            edges=[("s", "a"), ("a", "b"), ("b", "a"), ("t", "b")],
        )
        d = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d.removed, frozenset())
        self.assertEqual(op.reachable, frozenset({"s", "t", "a", "b"}))

    def test_second_seed_keeps_only_its_downstream(self):
        # Cycle a<->b with entry s->a; t enters only a.  Removing s->a keeps a
        # via t, and b via a.  Then removing t->a retracts both.
        op = IncrementalReachability(
            seeds={"s", "t"}, edges=[("s", "a"), ("a", "b"), ("b", "a"), ("t", "a")]
        )
        d1 = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d1.removed, frozenset())
        d2 = op.apply(edges_remove=[("t", "a")])
        self.assertEqual(d2.removed, frozenset({"a", "b"}))

    def test_seed_added_inside_cycle_keeps_it_after_entry_removed(self):
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "a")]
        )
        d = op.apply(seeds_add=["b"])
        # "b" was already reachable, so the reachable set does not grow.
        self.assertEqual(d.added, frozenset())
        self.assertEqual(d.removed, frozenset())
        d2 = op.apply(edges_remove=[("s", "a")])
        self.assertEqual(d2.removed, frozenset())
        self.assertEqual(op.reachable, frozenset({"s", "b", "a"}))

    def test_seed_added_inside_cycle_in_same_batch_as_entry_removal(self):
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "a")]
        )
        d = op.apply(seeds_add=["b"], edges_remove=[("s", "a")])
        self.assertEqual(d.removed, frozenset())
        self.assertEqual(op.reachable, frozenset({"s", "b", "a"}))

    def test_remove_seed_on_cycle_entry(self):
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "a")]
        )
        d = op.apply(seeds_remove=["s"])
        self.assertEqual(d.removed, frozenset({"s", "a", "b"}))


class SeedRemovalTests(unittest.TestCase):
    def test_remove_seed_without_other_support(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a"), ("a", "b")])
        d = op.apply(seeds_remove=["s"])
        self.assertEqual(d.removed, frozenset({"s", "a", "b"}))
        self.assertEqual(op.reachable, frozenset())

    def test_remove_seed_keeps_nodes_reached_by_other_seed(self):
        op = IncrementalReachability(
            seeds={"s1", "s2"},
            edges=[("s1", "a"), ("a", "b"), ("s2", "b")],
        )
        d = op.apply(seeds_remove=["s1"])
        # Both roots and "a" (only reachable via s1) go; "b" stays via s2.
        self.assertEqual(d.removed, frozenset({"s1", "a"}))
        self.assertEqual(op.reachable, frozenset({"s2", "b"}))

    def test_remove_all_seeds(self):
        op = IncrementalReachability(seeds={"s1", "s2"}, edges=[("s1", "a"), ("s2", "b")])
        d = op.apply(seeds_remove=["s1", "s2"])
        self.assertEqual(d.removed, frozenset({"s1", "s2", "a", "b"}))
        self.assertEqual(op.reachable, frozenset())

    def test_add_seed_then_remove_restores(self):
        op = IncrementalReachability(seeds={"s"}, edges=[("s", "a"), ("x", "a")])
        d1 = op.apply(seeds_add=["x"])
        self.assertEqual(d1.added, frozenset({"x"}))
        d2 = op.apply(seeds_remove=["x"])
        self.assertEqual(d2.removed, frozenset({"x"}))
        self.assertEqual(op.reachable, frozenset({"s", "a"}))


class LocalizedExpansionTests(unittest.TestCase):
    def test_disjoint_component_not_traversed(self):
        # Two seeds, two totally separate components.
        op = _Instrumented(
            seeds={"s"},
            edges=[("s", "a"), ("a", "b"),
                   ("u", "v"), ("v", "w")],  # u,v,w unreachable component
        )
        op.apply(seeds_add=["u"])  # make the second component live too
        op.expanded.clear()

        d = op.apply(edges_add=[("b", "b2")])
        self.assertNotIn("u", op.expanded)
        self.assertNotIn("v", op.expanded)
        self.assertNotIn("w", op.expanded)
        self.assertEqual(d.reexpanded, frozenset({"b2"}))

    def test_reexpanded_only_covers_affected_region(self):
        op = _Instrumented(
            seeds={"s", "t"},
            edges=[("s", "a"), ("a", "b"), ("t", "x"), ("x", "y")],
        )
        op.expanded.clear()
        d = op.apply(edges_remove=[("a", "b")])
        self.assertNotIn("x", d.reexpanded)
        self.assertNotIn("y", d.reexpanded)
        self.assertNotIn("x", op.expanded)
        self.assertIn("b", d.removed)

    def test_removed_cycle_not_reexpanded_afterwards(self):
        op = _Instrumented(
            seeds={"s"}, edges=[("s", "a"), ("a", "b"), ("b", "a")]
        )
        d = op.apply(edges_remove=[("s", "a")])
        # The cycle retracts; it is not re-expanded (nothing reachable there).
        self.assertEqual(d.reexpanded, frozenset())
        self.assertEqual(d.removed, frozenset({"a", "b"}))

    def test_reexpanded_nodes_are_reachable_after_batch(self):
        op = IncrementalReachability(
            seeds={"s"}, edges=[("s", "a"), ("a", "b")]
        )
        d = op.apply(edges_remove=[("a", "b")])
        for node in d.reexpanded:
            self.assertIn(node, op.reachable)

    def test_unrelated_component_untouched_by_seed_change(self):
        op = _Instrumented(
            seeds={"s"},
            edges=[("s", "a"), ("u", "v")],
        )
        op.expanded.clear()
        op.apply(seeds_remove=["s"])
        self.assertNotIn("u", op.expanded)
        self.assertNotIn("v", op.expanded)

    def test_large_dead_island_does_not_slow_local_update(self):
        # Guard against accidental global sweeps: a 2k-edge disconnected
        # island must not show up in re-expansion when one chain node changes.
        edges = [("s", "n0")] + [
            ("n%d" % i, "n%d" % (i + 1)) for i in range(199)
        ] + [("d%d" % i, "d%d" % (i + 1)) for i in range(2000)]
        op = _Instrumented(seeds={"s"}, edges=edges)
        op.expanded.clear()
        d = op.apply(edges_remove=[("n5", "n6")])
        for i in range(2000):
            self.assertNotIn("d%d" % i, op.expanded)
            self.assertNotIn("d%d" % i, d.reexpanded)
        # Only the retracted tail of the live chain is re-examined.
        self.assertEqual(
            d.removed, frozenset("n%d" % i for i in range(6, 200))
        )
        self.assertEqual(d.reexpanded, frozenset())


class ReplayTests(unittest.TestCase):
    def test_replay_after_each_delta_matches_operator(self):
        rng = random.Random(1234)
        nodes = list(range(12))
        op = IncrementalReachability(seeds={0, 1})
        replay = set(op.reachable)

        all_edges = set()
        for _ in range(80):
            kind = rng.random()
            if kind < 0.5:
                e = (rng.choice(nodes), rng.choice(nodes))
                d = op.apply(edges_add=[e])
                all_edges.add(e)
            elif kind < 0.75:
                if all_edges:
                    e = rng.choice(sorted(all_edges))
                    all_edges.discard(e)
                    d = op.apply(edges_remove=[e])
                else:
                    d = op.apply()
            elif kind < 0.9:
                d = op.apply(seeds_add=[rng.choice(nodes)])
            else:
                if op.seeds:
                    d = op.apply(seeds_remove=[rng.choice(sorted(op.seeds))])
                else:
                    d = op.apply()

            replay = d.replay_onto(replay)
            self.assertEqual(replay, set(op.reachable))
            self.assertTrue(d.added.isdisjoint(d.removed))


class ConsistencyTests(unittest.TestCase):
    """Differential test: incremental deltas match a full recomputation."""

    def test_random_batches_match_full_recompute(self):
        for seed in (99, 7, 2024):
            self._differential(random.Random(seed))

    def _differential(self, rng):
        nodes = list(range(10))
        seeds = {0}
        edges = set()

        op = IncrementalReachability(seeds=seeds, edges=edges)
        for step in range(300):
            sa, sr, ea, er = set(), set(), set(), set()
            for _ in range(rng.randint(0, 3)):
                r = rng.random()
                node = rng.choice(nodes)
                if r < 0.5:
                    sa.add(node)
                else:
                    sr.add(node)
            for _ in range(rng.randint(0, 4)):
                e = (rng.choice(nodes), rng.choice(nodes))
                if rng.random() < 0.5:
                    ea.add(e)
                else:
                    er.add(e)

            old = set(op.reachable)
            d = op.apply(seeds_add=sa, seeds_remove=sr,
                         edges_add=ea, edges_remove=er)

            # Maintain the reference model with remove-then-add semantics.
            seeds = (seeds - sr) | sa
            edges = (edges - er) | ea
            expected = full_reachable(seeds, edges)

            self.assertEqual(set(op.reachable), expected,
                             msg=f"step {step} batch {(sa, sr, ea, er)}")
            self.assertEqual((old - set(d.removed)) | set(d.added), expected)
            self.assertEqual(set(d.added), expected - old)
            self.assertEqual(set(d.removed), old - expected)
            self.assertEqual(set(op.seeds), seeds)


if __name__ == "__main__":
    unittest.main()
