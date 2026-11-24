"""Tests for the versioned catalog and fixed-version snapshots."""

import unittest

from plan_cache import (
    Catalog,
    NamespaceNotFound,
    RelationNotFound,
)


class CatalogVersioningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = Catalog()
        self.catalog.create_namespace("app")
        self.catalog.create_namespace("shared")

    def test_every_ddl_event_advances_the_version_and_is_logged(self) -> None:
        self.assertEqual(self.catalog.current_version, 2)
        create = self.catalog.create_table("app", "orders")
        self.assertEqual(create.version, 3)
        self.assertEqual(create.kind, "create_table")
        self.assertEqual(create.relation_oid, 1)
        drop = self.catalog.drop_table("app", "orders")
        self.assertEqual(drop.version, 4)
        self.assertEqual(drop.kind, "drop_table")
        self.assertEqual(drop.relation_oid, create.relation_oid)
        kinds = [e.kind for e in self.catalog.events()]
        self.assertEqual(
            kinds,
            [
                "create_namespace",
                "create_namespace",
                "create_table",
                "drop_table",
            ],
        )

    def test_oids_are_stable_and_unique(self) -> None:
        first = self.catalog.create_table("app", "orders").relation_oid
        second = self.catalog.create_table("shared", "orders").relation_oid
        third = self.catalog.create_table("app", "events").relation_oid
        self.assertEqual(len({first, second, third}), 3)
        # A re-created same-named table gets a brand new object number.
        self.catalog.drop_table("app", "orders")
        reborn = self.catalog.create_table("app", "orders").relation_oid
        self.assertNotEqual(reborn, first)

    def test_duplicate_and_missing_namespace_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.catalog.create_namespace("app")
        with self.assertRaises(NamespaceNotFound):
            self.catalog.create_table("ghost", "orders")

    def test_snapshot_range_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.catalog.snapshot(-1)
        with self.assertRaises(ValueError):
            self.catalog.snapshot(self.catalog.current_version + 1)


class SnapshotResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = Catalog()
        self.catalog.create_namespace("app")       # v1
        self.catalog.create_namespace("shared")    # v2
        self.catalog.create_table("shared", "orders")  # v3 -> oid 1
        self.version_before_shadow = self.catalog.current_version
        self.catalog.create_table("app", "orders")     # v4 -> oid 2

    def test_search_path_first_match_wins(self) -> None:
        snap = self.catalog.snapshot(self.catalog.current_version)
        first = snap.resolve("orders", ("app", "shared"))
        self.assertEqual((first.oid, first.namespace, first.position),
                         (2, "app", 0))
        second = snap.resolve("orders", ("shared", "app"))
        self.assertEqual((second.oid, second.namespace, second.position),
                         (1, "shared", 0))

    def test_old_snapshot_does_not_see_later_tables(self) -> None:
        old = self.catalog.snapshot(self.version_before_shadow)
        resolved = old.resolve("orders", ("app", "shared"))
        self.assertEqual(resolved.oid, 1)
        self.assertEqual(resolved.namespace, "shared")
        self.assertEqual(resolved.position, 1)

    def test_drop_is_invisible_before_its_version_and_visible_after(self) -> None:
        drop_version = self.catalog.drop_table("app", "orders").version
        before = self.catalog.snapshot(drop_version - 1)
        self.assertEqual(
            before.resolve("orders", ("app", "shared")).oid, 2
        )
        after = self.catalog.snapshot(drop_version)
        # The app table is gone, so the shared table wins again.
        self.assertEqual(
            after.resolve("orders", ("app", "shared")).oid, 1
        )

    def test_missing_namespaces_on_the_path_are_skipped(self) -> None:
        snap = self.catalog.snapshot(1)  # only "app" exists, and it is empty
        with self.assertRaises(RelationNotFound):
            snap.resolve("orders", ("app", "shared"))
        # At v2 "shared" exists but holds no table yet either.
        with self.assertRaises(RelationNotFound):
            self.catalog.snapshot(2).resolve(
                "orders", ("app", "shared")
            )

    def test_events_between_is_half_open(self) -> None:
        events = self.catalog.events_between(2, 3)
        self.assertEqual([e.version for e in events], [3])
        events = self.catalog.events_between(3, 4)
        self.assertEqual([e.version for e in events], [4])
        self.assertEqual(self.catalog.events_between(4, 4), ())


if __name__ == "__main__":
    unittest.main()
