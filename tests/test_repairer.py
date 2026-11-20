"""Local synthetic-data tests for the prefix-summary repairer.

All data is generated in memory; there are no network calls, no external
services and no on-disk state.  NOTE: per the task constraints these tests
were authored but NOT executed in this round (see README.md); they are plain
``unittest`` cases intended to be run later with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from repairer import (
    KEY_BITS,
    KEY_COUNT,
    canonical_summary,
    empty_leaf_digest,
    empty_subtree_digest,
    key_to_prefix,
    leaf_digest,
    node_digest,
    prefix_range,
    reconcile,
)
from repairer.store import MemoryStore


class DigestRulesTest(unittest.TestCase):
    def test_empty_leaf_sentinel_is_fixed(self):
        self.assertEqual(empty_leaf_digest(), empty_leaf_digest())
        self.assertNotEqual(empty_leaf_digest(), leaf_digest(b""))

    def test_left_right_partition_is_ordered(self):
        a = leaf_digest(b"a")
        b = leaf_digest(b"b")
        self.assertNotEqual(node_digest(a, b), node_digest(b, a))

    def test_empty_subtree_digests(self):
        self.assertEqual(empty_subtree_digest(KEY_BITS), empty_leaf_digest())
        for depth in range(KEY_BITS):
            self.assertEqual(
                empty_subtree_digest(depth),
                node_digest(
                    empty_subtree_digest(depth + 1),
                    empty_subtree_digest(depth + 1),
                ),
            )

    def test_prefix_range_partitions_the_space(self):
        self.assertEqual(prefix_range(""), (0, KEY_COUNT))
        self.assertEqual(prefix_range("0"), (0, KEY_COUNT // 2))
        self.assertEqual(prefix_range("1"), (KEY_COUNT // 2, KEY_COUNT))
        leaf = key_to_prefix(0b0000000000000101)
        self.assertEqual(prefix_range(leaf), (5, 6))

    def test_canonical_summary_is_shape_independent(self):
        entries = {1: b"v1", 2: b"v2", 65535: b"last", 4096: b"mid"}
        fine = MemoryStore.fine(entries, label="fine")
        folded = MemoryStore.folded(entries, fold_depth=3, label="folded")
        s_fine = fine.snapshot()
        s_folded = folded.snapshot()
        for prefix in ("", "0", "1", "0000", "1111", "0001000", key_to_prefix(1),
                       key_to_prefix(65535), key_to_prefix(4096)):
            self.assertEqual(
                s_fine.summary(prefix),
                s_folded.summary(prefix),
                prefix,
            )
            self.assertEqual(
                s_fine.summary(prefix),
                canonical_summary(entries, prefix),
            )

    def test_summaries_differ_when_values_differ(self):
        a = {7: b"x"}
        b = {7: b"y"}
        self.assertNotEqual(
            canonical_summary(a, ""), canonical_summary(b, "")
        )
        # Same value under different key -> different root summary too.
        c = {8: b"x"}
        self.assertNotEqual(
            canonical_summary(a, ""), canonical_summary(c, "")
        )


class ReconcileTest(unittest.TestCase):
    def assertConverged(self, source: MemoryStore, target: MemoryStore):
        self.assertEqual(source.entries, target.entries)
        # A second run must find nothing to do and only probe the root.
        report = reconcile(source, target)
        self.assertEqual(report.repair_count, 0)
        self.assertEqual(report.probed_prefixes, [""])

    def test_identical_trees_skip_everything(self):
        entries = {1: b"a", 100: b"b", 60000: b"c"}
        source = MemoryStore.fine(entries)
        target = MemoryStore.folded(entries, fold_depth=2)
        report = reconcile(source, target)
        self.assertEqual(report.repair_count, 0)
        self.assertEqual(report.probed_prefixes, [""])
        self.assertEqual(report.source_body_reads, ())
        self.assertEqual(report.target_body_reads, ())

    def test_single_overwrite_different_shapes_reads_one_body(self):
        common = {1: b"a", 2: b"b", 1000: b"c", 40000: b"d"}
        source = MemoryStore.fine({**common, 1000: b"new"})
        target = MemoryStore.folded(common, fold_depth=4)
        report = reconcile(source, target)

        self.assertEqual(report.repair_count, 1)
        repair = report.repairs[0]
        self.assertEqual((repair.key, repair.action, repair.value), (1000, "put", b"new"))
        # Exactly one object body read, on the source side, for that key.
        self.assertEqual(report.source_body_reads, (1000,))
        self.assertEqual(report.target_body_reads, ())
        # Root, then at each of the 16 depths both fixed children are probed:
        # the equal sibling is pruned immediately, the differing child is
        # descended into.  1 + 2 * KEY_BITS summary probes in total, and the
        # differing leaf is the last prefix visited.
        self.assertEqual(len(report.probed_prefixes), 1 + 2 * KEY_BITS)
        self.assertEqual(report.probed_prefixes[-1], key_to_prefix(1000))
        self.assertConverged(source, target)

    def test_target_only_key_is_deleted_without_any_body_read(self):
        source = MemoryStore.fine({1: b"a", 2: b"b"})
        target = MemoryStore.folded({1: b"a", 2: b"b", 555: b"extra"}, fold_depth=1)
        report = reconcile(source, target)

        self.assertEqual(report.delete_count, 1)
        self.assertEqual(report.put_count, 0)
        self.assertEqual(report.repairs[0].key, 555)
        self.assertEqual(report.source_body_reads, ())
        self.assertEqual(report.target_body_reads, ())
        self.assertConverged(source, target)

    def test_source_only_key_is_inserted_with_single_source_read(self):
        source = MemoryStore.fine({1: b"a", 9: b"fresh"})
        target = MemoryStore.folded({1: b"a"}, fold_depth=8)
        report = reconcile(source, target)

        self.assertEqual(report.put_count, 1)
        self.assertEqual(report.repairs[0].key, 9)
        self.assertEqual(report.source_body_reads, (9,))
        self.assertEqual(report.target_body_reads, ())
        self.assertConverged(source, target)

    def test_mixed_changes_between_completely_different_shapes(self):
        base = {10: b"same", 20: b"old", 30: b"gone"}
        source_entries = {10: b"same", 20: b"changed", 40: b"added"}
        # Fine-grained source, aggressively folded target (root + depth-1 only).
        source = MemoryStore.fine(source_entries)
        target = MemoryStore.folded(base, fold_depth=1)
        self.assertNotEqual(
            source.materialized_prefixes, target.materialized_prefixes
        )

        report = reconcile(source, target)
        actions = {(r.key, r.action): r.value for r in report.repairs}
        self.assertEqual(
            actions,
            {
                (20, "put"): b"changed",
                (30, "delete"): None,
                (40, "put"): b"added",
            },
        )
        # Bodies are read only for the two present-but-differing source keys.
        self.assertEqual(sorted(report.source_body_reads), [20, 40])
        # The target side never yields an object body.
        self.assertEqual(report.target_body_reads, ())
        self.assertConverged(source, target)

    def test_empty_source_clears_target_with_no_body_reads(self):
        source = MemoryStore.fine({})
        target = MemoryStore.fine({0: b"x", 65535: b"y", 32768: b"z"}, fold_depth=2)
        report = reconcile(source, target)
        self.assertEqual(report.delete_count, 3)
        self.assertEqual(report.put_count, 0)
        self.assertEqual(report.source_body_reads, ())
        self.assertEqual(report.target_body_reads, ())
        self.assertConverged(source, target)

    def test_both_empty_only_probes_root(self):
        report = reconcile(MemoryStore.fine({}), MemoryStore.folded({}, fold_depth=0))
        self.assertEqual(report.repair_count, 0)
        self.assertEqual(report.probed_prefixes, [""])

    def test_dry_run_does_not_mutate_target(self):
        source = MemoryStore.fine({1: b"new"})
        target = MemoryStore.folded({1: b"old", 2: b"x"}, fold_depth=4)
        before = dict(target.entries)
        report = reconcile(source, target, apply=False)
        self.assertEqual(report.repair_count, 2)
        self.assertEqual(target.entries, before)

    def test_dense_subtree_single_change_still_reads_one_body(self):
        # 256 keys packed under prefix 00000000, then one value changes.
        base = {i: b"v%04d" % i for i in range(256)}
        changed = dict(base)
        changed[100] = b"mutated"
        source = MemoryStore.fine(changed)
        target = MemoryStore.folded(base, fold_depth=8)
        report = reconcile(source, target)
        self.assertEqual(report.repair_count, 1)
        self.assertEqual(report.source_body_reads, (100,))
        self.assertEqual(report.target_body_reads, ())
        self.assertConverged(source, target)

    def test_target_body_is_never_read_in_worst_case(self):
        # Every key differs: target still contributes zero body reads.
        source = MemoryStore.fine({i: b"s%d" % i for i in range(0, 512, 2)})
        target = MemoryStore.folded(
            {i: b"t%d" % i for i in range(1, 512, 2)}, fold_depth=2
        )
        report = reconcile(source, target)
        self.assertEqual(report.target_body_reads, ())
        self.assertEqual(
            sorted(report.source_body_reads),
            sorted(source.entries.keys()),
        )
        self.assertConverged(source, target)

    def test_snapshots_are_isolated_from_later_writes(self):
        store = MemoryStore.fine({1: b"a"})
        snap = store.snapshot()
        root_before = snap.summary("")
        store.put(2, b"b")
        store.delete(1)
        self.assertEqual(snap.entries, {1: b"a"})
        self.assertEqual(snap.summary(""), root_before)
        self.assertEqual(snap.get(2), None)
        self.assertEqual(snap.get(1), b"a")

    def test_probed_prefixes_are_valid_and_shared_both_sides(self):
        source = MemoryStore.fine({0: b"a", 65535: b"b"})
        target = MemoryStore.folded({0: b"a", 65535: b"c"}, fold_depth=3)
        report = reconcile(source, target)
        for prefix in report.probed_prefixes:
            self.assertLessEqual(len(prefix), KEY_BITS)
            self.assertTrue(all(ch in "01" for ch in prefix))
        self.assertEqual(
            report.source_snapshot.summary_prefixes,
            report.target_snapshot.summary_prefixes,
        )
        self.assertEqual(
            report.source_snapshot.summary_prefixes,
            tuple(report.probed_prefixes),
        )

    def test_custom_materialization_keeps_canonical_summaries(self):
        entries = {5: b"a", 6: b"b", 700: b"c"}
        # Odd, non-standard materialization set (root plus two arbitrary nodes).
        store = MemoryStore(entries, materialized=["", "000", "10101"])
        snap = store.snapshot()
        for prefix in ("", "0", "000", "111", key_to_prefix(5), key_to_prefix(700)):
            self.assertEqual(
                snap.summary(prefix), canonical_summary(entries, prefix)
            )


if __name__ == "__main__":
    unittest.main()
