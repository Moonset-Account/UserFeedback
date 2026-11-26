import tempfile
import unittest
from pathlib import Path

from column_migration import (
    BackfillConflict,
    BackfillJob,
    ColumnMigrationStore,
    DefinitionRegistry,
)


class RowVersionOCCTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = ColumnMigrationStore.create(
            self.root, registry=DefinitionRegistry.with_score_versions()
        )
        self.store.import_rows("g0", [{"id": "a0", "x": 1}, {"id": "a1", "x": 2}])
        self.store.import_rows("g1", [{"id": "b0", "x": 3}, {"id": "b1", "x": 4}])
        self.store.add_computed_column("score", 1)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_unrelated_newer_snapshot_does_not_block_cas(self) -> None:
        job = self.store.prepare_backfill("g0")
        self.store.update_rows({"b0": {"x": 300}})

        # Publication compares row versions, not the base snapshot id.
        snapshot_id = self.store.publish_backfill(job)
        read = self.store.read_snapshot(snapshot_id)
        self.assertEqual(read.materialized_groups, ["g0"])
        self.assertEqual(read.get("a0")["source_version"], 1)
        self.assertEqual(read.get("b0")["source_version"], 2)

    def test_late_update_rejects_stale_backfill_after_reopen(self) -> None:
        job = self.store.prepare_backfill("g1")
        self.assertEqual(job.read_versions, {"b0": 1, "b1": 1})

        self.store.update_rows({"b0": {"x": 30}})
        token = job.to_token()

        reopened = ColumnMigrationStore.open(
            self.root, registry=DefinitionRegistry.with_score_versions()
        )
        with self.assertRaises(BackfillConflict) as ctx:
            reopened.publish_backfill(BackfillJob.from_token(token))

        self.assertEqual(ctx.exception.read_versions, {"b0": 1, "b1": 1})
        self.assertEqual(ctx.exception.current_versions, {"b0": 2, "b1": 1})

        # The rejected computed file is not visible through the manifest.
        read = reopened.read_snapshot()
        self.assertEqual(read.materialized_groups, [])
        self.assertEqual(read.get("b0")["value"], 60)  # v1 formula on x=30
        self.assertEqual(read.get("b0")["source_version"], 2)

        fresh = reopened.prepare_backfill("g1")
        reopened.publish_backfill(fresh)
        self.assertTrue(reopened.read_snapshot().get("b0")["materialized"])
        self.assertTrue(reopened.verify_all_snapshots().ok)

    def test_whole_group_rewrite_bumps_all_row_versions_and_conflicts(self) -> None:
        job = self.store.prepare_backfill("g1")
        self.store.rewrite_group("g1")  # same logical rows, new generation

        with self.assertRaises(BackfillConflict) as ctx:
            self.store.publish_backfill(job)
        self.assertEqual(ctx.exception.current_versions, {"b0": 2, "b1": 2})

        fresh = self.store.prepare_backfill("g1")
        self.store.publish_backfill(fresh)
        row = self.store.read_snapshot().get("b1")
        self.assertTrue(row["materialized"])
        self.assertEqual(row["source_version"], 2)
        self.assertEqual(row["value"], 8)

    def test_multi_group_update_uses_one_manifest_snapshot(self) -> None:
        before = self.store.current_snapshot_id
        snapshot_id = self.store.update_rows(
            {"a0": {"x": 100}, "b0": {"x": 200}}
        )
        self.assertEqual(snapshot_id, before + 1)

        read = self.store.read_snapshot(snapshot_id)
        self.assertEqual(read.get("a0")["source_version"], 2)
        self.assertEqual(read.get("b0")["source_version"], 2)
        self.assertEqual(read.get("a1")["source_version"], 1)
        self.assertEqual(read.get("b1")["source_version"], 1)
        self.assertEqual(read.get("a0")["value"], 200)
        self.assertEqual(read.get("b0")["value"], 400)
        self.assertTrue(self.store.verify_all_snapshots().ok)

    def test_update_after_successful_publish_falls_back_to_declared_version(self) -> None:
        self.store.publish_backfill(self.store.prepare_backfill("g0"))
        self.store.update_rows({"a0": {"x": 100}})

        row = self.store.read_snapshot().get("a0")
        self.assertFalse(row["materialized"])
        self.assertEqual(row["fallback_reason"], "stale_source_version")
        self.assertEqual(row["source_version"], 2)
        self.assertEqual(row["value"], 200)

        # The unchanged sibling is also lazy because the group chunk is gated by
        # its complete row-version map.
        sibling = self.store.read_snapshot().get("a1")
        self.assertFalse(sibling["materialized"])
        self.assertEqual(sibling["source_version"], 1)

        report = self.store.verify()
        self.assertTrue(report.ok, report.errors)


if __name__ == "__main__":
    unittest.main()
