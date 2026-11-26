import tempfile
import unittest
from pathlib import Path

from column_migration import ColumnMigrationStore, DefinitionRegistry


class DefinitionUpgradeAndUndoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = ColumnMigrationStore.create(
            self.root, registry=DefinitionRegistry.with_score_versions()
        )
        self.store.import_rows("g0", [{"id": "a0", "x": 1}, {"id": "a1", "x": 2}])
        self.store.import_rows("g1", [{"id": "b0", "x": 4}])
        self.v1 = self.store.add_computed_column("score", 1, migration_id="score-v1")
        self.store.publish_backfill(self.store.prepare_backfill("g0"))
        self.v1_snapshot = self.store.current_snapshot_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_old_snapshot_keeps_v1_after_definition_upgrade(self) -> None:
        v2 = self.store.upgrade_definition("score", 2, migration_id="score-v2")
        self.assertEqual(v2, "score-v2")

        old = self.store.read_snapshot(self.v1_snapshot)
        self.assertEqual(old.definition_ref, "score@v1")
        self.assertEqual(old.materialized_groups, ["g0"])
        self.assertEqual(old.lazy_groups, ["g1"])
        self.assertEqual(old.get("a0")["value"], 2)
        self.assertEqual(old.get("b0")["value"], 8)

        current = self.store.read_snapshot()
        self.assertEqual(current.definition_ref, "score@v2")
        # The v1 chunk must not be reused as if it were v2.
        self.assertEqual(current.materialized_groups, [])
        self.assertEqual(current.lazy_groups, ["g0", "g1"])
        self.assertEqual(current.get("a0")["value"], 3)  # 2*x + 1
        self.assertEqual(current.get("b0")["value"], 9)

        reopened = ColumnMigrationStore.open(
            self.root, registry=DefinitionRegistry.with_score_versions()
        )
        old_after_reopen = reopened.read_snapshot(self.v1_snapshot)
        self.assertEqual(old_after_reopen.definition_version, 1)
        self.assertTrue(old_after_reopen.get("a0")["materialized"])
        self.assertEqual(old_after_reopen.get("a0")["value"], 2)
        self.assertTrue(reopened.verify_all_snapshots().ok)

    def test_v2_backfill_and_group_undo(self) -> None:
        self.store.upgrade_definition("score", 2, migration_id="score-v2")
        self.store.publish_backfill(self.store.prepare_backfill("g0"))
        v2_backfilled_sid = self.store.current_snapshot_id

        materialized = self.store.read_snapshot(v2_backfilled_sid)
        self.assertEqual(materialized.materialized_groups, ["g0"])
        self.assertEqual(materialized.get("a0")["value"], 3)

        undo_sid = self.store.undo_group_migration("g0", migration_id="score-v2")
        undone = self.store.read_snapshot(undo_sid)
        self.assertEqual(undone.materialized_groups, [])
        self.assertEqual(undone.lazy_groups, ["g0", "g1"])
        row = undone.get("a0")
        self.assertFalse(row["materialized"])
        self.assertEqual(row["fallback_reason"], "undone")
        self.assertEqual(row["definition_version"], 2)
        self.assertEqual(row["value"], 3)

        # The snapshot immediately before undo is still intact.
        before = self.store.read_snapshot(v2_backfilled_sid)
        self.assertTrue(before.get("a0")["materialized"])

        reopened = ColumnMigrationStore.open(
            self.root, registry=DefinitionRegistry.with_score_versions()
        )
        self.assertEqual(
            reopened.read_snapshot(undo_sid).get("a0")["fallback_reason"],
            "undone",
        )
        self.assertTrue(reopened.verify_all_snapshots().ok)

    def test_undo_v1_group_returns_to_declared_lazy_reads(self) -> None:
        undo_sid = self.store.undo_group_migration("g0", migration_id=self.v1)
        read = self.store.read_snapshot(undo_sid)
        self.assertEqual(read.materialized_groups, [])
        row = read.get("a1")
        self.assertEqual(row["fallback_reason"], "undone")
        self.assertEqual(row["value"], 4)  # v1 declared formula

        before = self.store.read_snapshot(self.v1_snapshot)
        self.assertTrue(before.get("a1")["materialized"])
        self.assertTrue(self.store.verify_all_snapshots().ok)


class RetentionReclamationTest(unittest.TestCase):
    def test_pinned_snapshot_files_are_protected_from_reclamation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ColumnMigrationStore.create(
                root, registry=DefinitionRegistry.with_score_versions()
            )
            store.import_rows("g0", [{"id": "a0", "x": 1}])
            store.add_computed_column("score", 1, migration_id="score-v1")
            store.publish_backfill(store.prepare_backfill("g0"))
            pinned_sid = store.current_snapshot_id
            manifest = store.load_manifest(pinned_sid)
            source_rel = manifest["groups"]["g0"]["source"]["path"]
            computed_rel = manifest["groups"]["g0"]["backfills"]["score-v1"]["file"]["path"]
            store.pin_snapshot(pinned_sid)

            store.update_rows({"a0": {"x": 2}})
            store.publish_backfill(store.prepare_backfill("g0"))
            store.prune_snapshots(keep_latest=1)
            removed = store.reclaim_unreferenced_files()

            self.assertTrue((root / source_rel).exists())
            self.assertTrue((root / computed_rel).exists())
            self.assertNotIn(source_rel, removed)
            pinned_read = store.read_snapshot(pinned_sid)
            self.assertEqual(pinned_read.get("a0")["source_version"], 1)
            self.assertEqual(pinned_read.get("a0")["value"], 2)
            self.assertTrue(store.verify_all_snapshots().ok)

            # Explicitly releasing/pruning that snapshot makes its old chunks
            # reclaimable; the current snapshot remains verifiable.
            store.unpin_snapshot(pinned_sid)
            store.prune_snapshots(keep_latest=1)
            store.reclaim_unreferenced_files()
            self.assertFalse((root / source_rel).exists())
            self.assertFalse((root / computed_rel).exists())
            current_report = store.verify()
            self.assertTrue(current_report.ok, current_report.errors)


if __name__ == "__main__":
    unittest.main()
