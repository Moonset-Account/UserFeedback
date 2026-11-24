"""Boundary and interleaving edge cases."""

import unittest

from plan_cache import (
    BindingCache,
    Catalog,
    RelationNotFound,
    Session,
)

STMT = "SELECT * FROM orders"
TABLE = "orders"


class EmptySearchPathTest(unittest.TestCase):
    def test_empty_path_never_resolves(self) -> None:
        catalog = Catalog()
        catalog.create_namespace("app")
        catalog.create_table("app", "orders")
        cache = BindingCache()
        snap = catalog.snapshot(catalog.current_version)
        with self.assertRaises(RelationNotFound):
            cache.lookup(TABLE, (), STMT, snap)
        # Nothing was cached for the unresolvable key.
        self.assertEqual(cache.history(TABLE, (), STMT), ())


class StatementIsolationTest(unittest.TestCase):
    def test_different_statement_text_uses_different_cache_entries(self) -> None:
        catalog = Catalog()
        catalog.create_namespace("app")
        catalog.create_table("app", "orders")
        cache = BindingCache()
        snap = catalog.snapshot(catalog.current_version)

        first = cache.lookup(TABLE, ("app",), STMT, snap)
        second = cache.lookup(
            TABLE, ("app",), "DELETE FROM orders WHERE id = $1", snap
        )
        self.assertTrue(second.rebound)
        self.assertNotEqual(
            cache.make_key(TABLE, ("app",), STMT),
            cache.make_key(TABLE, ("app",), "other"),
        )
        self.assertEqual(second.oid, first.oid)  # same object, separate entry


class OutOfOrderVersionPopulateTest(unittest.TestCase):
    def test_older_snapshot_populates_history_after_newer_binding(self) -> None:
        catalog = Catalog()
        catalog.create_namespace("app")
        catalog.create_namespace("shared")
        catalog.create_table("shared", "orders")  # oid 1, v3
        version_v3 = catalog.current_version
        catalog.create_table("app", "orders")     # oid 2, v4 (shadow)

        cache = BindingCache()
        new_snap = catalog.snapshot(catalog.current_version)  # sees v4
        old_snap = catalog.snapshot(version_v3)               # pinned at v3

        # Newer transaction binds first.
        new_binding = cache.lookup(TABLE, ("app", "shared"), STMT, new_snap)
        self.assertEqual(new_binding.oid, 2)
        # Older transaction binds later; history must stay version ordered.
        old_binding = cache.lookup(TABLE, ("app", "shared"), STMT, old_snap)
        self.assertEqual(old_binding.oid, 1)

        history = cache.history(TABLE, ("app", "shared"), STMT)
        self.assertEqual(
            [(b.catalog_version, b.oid) for b in history],
            [(version_v3, 1), (catalog.current_version, 2)],
        )

        # A repeat lookup by either transaction is a stable hit.
        self.assertEqual(
            cache.lookup(TABLE, ("app", "shared"), STMT, old_snap).oid, 1
        )
        self.assertTrue(
            cache.lookup(TABLE, ("app", "shared"), STMT, old_snap).hit
        )
        self.assertEqual(
            cache.lookup(TABLE, ("app", "shared"), STMT, new_snap).oid, 2
        )


class NamespaceCreatedAfterSnapshotTest(unittest.TestCase):
    def test_namespace_born_after_snapshot_is_skipped_on_path(self) -> None:
        catalog = Catalog()
        catalog.create_namespace("shared")
        catalog.create_table("shared", "orders")  # oid 1
        pinned_version = catalog.current_version
        catalog.create_namespace("app")
        catalog.create_table("app", "orders")     # oid 2

        session = Session(
            catalog,
            BindingCache(),
            search_path=("app", "shared"),
        )
        # Begin at the old version: "app" does not exist yet for this txn.
        session.begin(snapshot_version=pinned_version)
        stmt = session.prepare(STMT, TABLE)
        result = session.execute(stmt)
        self.assertEqual(result.oid, 1)
        self.assertEqual(result.binding.namespace, "shared")
        session.commit()

        session.begin()
        moved = session.execute(stmt)
        self.assertEqual(moved.oid, 2)
        session.commit()


class CatalogEventsAreImmutableTest(unittest.TestCase):
    def test_event_log_grows_append_only(self) -> None:
        catalog = Catalog()
        catalog.create_namespace("app")
        event = catalog.create_table("app", "orders")
        with self.assertRaises(AttributeError):
            event.version = 999  # frozen dataclass
        log = catalog.events()
        self.assertEqual(len(log), 2)
        with self.assertRaises((AttributeError, TypeError)):
            log[0] = event  # tuple is immutable


if __name__ == "__main__":
    unittest.main()
