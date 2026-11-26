import tempfile
import unittest
from pathlib import Path

from column_migration import ColumnMigrationStore, DefinitionRegistry


class MixedSnapshotReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = DefinitionRegistry.with_score_versions()
        self.store = ColumnMigrationStore.create(
            self.root, registry=self.registry
        )
        self.store.import_rows(
            "g0",
            [
                {"id": "a0", "x": 1},
                {"id": "a1", "x": 3},
            ],
        )
        self.store.import_rows(
            "g1",
            [
                {"id": "b0", "x": 10},
                {"id": "b1", "x": 20},
            ],
        )
        self.migration_id = self.store.add_computed_column("score", 1)
        self.declared_sid = self.store.current_snapshot_id

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_backfilled_and_pending_groups_share_one_snapshot(self) -> None:
        job = self.store.prepare_backfill("g0")
        self.store.publish_backfill(job)

        read = self.store.read_snapshot()
        self.assertEqual(read.definition_ref, "score@v1")
        self.assertEqual(read.materialized_groups, ["g0"])
        self.assertEqual(read.lazy_groups, ["g1"])
        self.assertEqual(len(read.rows), 4)

        by_key = read.by_key()
        self.assertTrue(by_key["a0"]["materialized"])
        self.assertFalse(by_key["b0"]["materialized"])
        self.assertEqual(by_key["a0"]["value"], 2)
        self.assertEqual(by_key["a1"]["value"], 6)
        self.assertEqual(by_key["b0"]["value"], 20)
        self.assertEqual(by_key["b1"]["value"], 40)

        snapshot_ids = {row["snapshot_id"] for row in read.rows}
        definition_versions = {row["definition_version"] for row in read.rows}
        self.assertEqual(snapshot_ids, {read.snapshot_id})
        self.assertEqual(definition_versions, {1})

        # Pending rows are explicitly computed by the declared v1 formula and
        # carry the exact source row version used.
        for key in ("b0", "b1"):
            row = by_key[key]
            self.assertEqual(row["read_mode"], "declared_definition")
            self.assertEqual(row["fallback_reason"], "pending")
            self.assertEqual(row["source_version"], 1)

        report = self.store.verify()
        self.assertTrue(report.ok, report.errors)

    def test_declaration_snapshot_reads_all_groups_as_pending(self) -> None:
        job = self.store.prepare_backfill("g0")
        self.store.publish_backfill(job)

        old = self.store.read_snapshot(self.declared_sid)
        self.assertEqual(old.snapshot_id, self.declared_sid)
        self.assertEqual(old.materialized_groups, [])
        self.assertEqual(old.lazy_groups, ["g0", "g1"])
        self.assertEqual(old.get("a0")["value"], 2)
        self.assertEqual(old.get("a0")["definition_version"], 1)

    def test_mixed_read_survives_reopen(self) -> None:
        self.store.publish_backfill(self.store.prepare_backfill("g0"))

        reopened = ColumnMigrationStore.open(
            self.root, registry=DefinitionRegistry.with_score_versions()
        )
        read = reopened.read_snapshot()
        self.assertEqual(read.materialized_groups, ["g0"])
        self.assertEqual(read.lazy_groups, ["g1"])
        self.assertEqual(read.get("b1")["value"], 40)
        report = reopened.verify_all_snapshots()
        self.assertTrue(report.ok, report.errors)


if __name__ == "__main__":
    unittest.main()
