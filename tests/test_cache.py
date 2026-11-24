"""Tests for the snapshot-aware BindingCache itself."""

import unittest

from plan_cache import (
    BindingCache,
    Catalog,
    HIT_FRESH,
    HIT_NO_RELEVANT_CHANGE,
    REBIND_DROPPED,
    REBIND_FIRST_USE,
    REBIND_RESOLUTION_CHANGED,
    REBIND_SHADOWED,
    RelationNotFound,
)

STMT = "SELECT * FROM orders WHERE id = $1"
TABLE = "orders"
PATH = ("app", "shared")


def build_catalog() -> Catalog:
    catalog = Catalog()
    catalog.create_namespace("app")
    catalog.create_namespace("shared")
    catalog.create_namespace("misc")
    catalog.create_table("shared", "orders")  # oid 1
    return catalog


class BindingCacheBasicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_catalog()
        self.cache = BindingCache()

    def test_first_use_rebinds_then_repeat_hits_fresh(self) -> None:
        snap = self.catalog.snapshot(self.catalog.current_version)
        first = self.cache.lookup(TABLE, PATH, STMT, snap)
        self.assertTrue(first.rebound)
        self.assertEqual(first.reason, REBIND_FIRST_USE)
        self.assertEqual(first.oid, 1)
        self.assertIsNone(first.previous_binding)

        second = self.cache.lookup(TABLE, PATH, STMT, snap)
        self.assertTrue(second.hit)
        self.assertEqual(second.reason, HIT_FRESH)
        self.assertEqual(second.oid, first.oid)

    def test_history_is_retained_per_key(self) -> None:
        snap = self.catalog.snapshot(self.catalog.current_version)
        self.cache.lookup(TABLE, PATH, STMT, snap)
        history = self.cache.history(TABLE, PATH, STMT)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].oid, 1)
        self.assertEqual(self.cache.history(TABLE, ("shared",), STMT), ())


class ShadowingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_catalog()
        self.cache = BindingCache()

    def test_new_transaction_rebinds_to_earlier_namespace_shadow(self) -> None:
        old_snap = self.catalog.snapshot(self.catalog.current_version)
        old = self.cache.lookup(TABLE, PATH, STMT, old_snap)
        self.assertEqual(old.oid, 1)
        self.assertEqual(old.binding.position, 1)

        # DDL after the old transaction started: same-named table in the
        # namespace that comes first on the search path.
        shadow = self.catalog.create_table("app", "orders")  # oid 2

        new_snap = self.catalog.snapshot(self.catalog.current_version)
        rebound = self.cache.lookup(TABLE, PATH, STMT, new_snap)
        self.assertTrue(rebound.rebound)
        self.assertEqual(rebound.reason, REBIND_SHADOWED)
        self.assertEqual(rebound.oid, shadow.relation_oid)
        self.assertEqual(rebound.binding.position, 0)
        self.assertEqual(rebound.previous_binding.oid, 1)
        self.assertEqual([e.version for e in rebound.events], [shadow.version])

    def test_old_transaction_keeps_hitting_the_original_object(self) -> None:
        old_txn = self.catalog.snapshot(self.catalog.current_version)
        bound = self.cache.lookup(TABLE, PATH, STMT, old_txn)
        self.assertEqual(bound.oid, 1)

        self.catalog.create_table("app", "orders")  # shadow, oid 2

        # The old transaction's snapshot version never moves: its binding
        # remains valid and is still served from the cache history.
        again = self.cache.lookup(TABLE, PATH, STMT, old_txn)
        self.assertTrue(again.hit)
        self.assertEqual(again.oid, 1)

        # And the new transaction independently binds to the shadow.
        new_txn = self.catalog.snapshot(self.catalog.current_version)
        moved = self.cache.lookup(TABLE, PATH, STMT, new_txn)
        self.assertTrue(moved.rebound)
        self.assertEqual(moved.oid, 2)

        # Order independence: running the old transaction again afterwards
        # still returns the old object, interleaved with the new one.
        interleaved_old = self.cache.lookup(TABLE, PATH, STMT, old_txn)
        self.assertTrue(interleaved_old.hit)
        self.assertEqual(interleaved_old.oid, 1)
        interleaved_new = self.cache.lookup(TABLE, PATH, STMT, new_txn)
        self.assertTrue(interleaved_new.hit)
        self.assertEqual(interleaved_new.oid, 2)

        history = self.cache.history(TABLE, PATH, STMT)
        self.assertEqual([b.oid for b in history], [1, 2])
        self.assertTrue(
            history[0].catalog_version < history[1].catalog_version
        )


class OutOfPathChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_catalog()
        self.cache = BindingCache()

    def test_catalog_change_outside_search_path_keeps_the_plan(self) -> None:
        bound_snap = self.catalog.snapshot(self.catalog.current_version)
        bound = self.cache.lookup(TABLE, PATH, STMT, bound_snap)

        # Same-named table created in a namespace not on this search path.
        event = self.catalog.create_table("misc", "orders")
        # And unrelated DDL in another off-path namespace.
        other = self.catalog.create_table("misc", "audit")

        new_snap = self.catalog.snapshot(self.catalog.current_version)
        reused = self.cache.lookup(TABLE, PATH, STMT, new_snap)
        self.assertTrue(reused.hit)
        self.assertEqual(reused.reason, HIT_NO_RELEVANT_CHANGE)
        self.assertEqual(reused.oid, bound.oid)
        self.assertEqual(reused.events, ())
        self.assertGreater(self.catalog.current_version, event.version)
        self.assertEqual(other.relation_oid, 3)

    def test_same_event_is_relevant_for_a_path_that_includes_namespace(self) -> None:
        self.cache.lookup(
            TABLE, PATH, STMT,
            self.catalog.snapshot(self.catalog.current_version),
        )
        self.catalog.create_table("app", "orders")  # shadow for PATH
        snap = self.catalog.snapshot(self.catalog.current_version)

        # Path without "app": no relevant change, plain hit.
        off_path = self.cache.lookup(
            TABLE, ("shared",), STMT + " -- v2", snap
        )
        self.assertEqual(off_path.reason, REBIND_FIRST_USE)
        again = self.cache.lookup(
            TABLE, ("shared",), STMT + " -- v2", snap
        )
        self.assertTrue(again.hit)

        # Path with "app": shadowing re-bind.
        shadowed = self.cache.lookup(TABLE, PATH, STMT, snap)
        self.assertEqual(shadowed.reason, REBIND_SHADOWED)


class DropTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_catalog()
        self.cache = BindingCache()

    def test_dropped_binding_rebinds_to_replacement(self) -> None:
        self.cache.lookup(
            TABLE, PATH, STMT,
            self.catalog.snapshot(self.catalog.current_version),
        )
        # Create then drop a shadow, then drop the originally bound table.
        self.catalog.create_table("app", "orders")  # oid 2
        shadow_drop = self.catalog.drop_table("app", "orders")
        drop = self.catalog.drop_table("shared", "orders")
        # Replacement appears later on the path. The dropped shadow was oid 2,
        # so this new table takes the next free oid (3).
        replacement = self.catalog.create_table("app", "orders")  # oid 3
        snap = self.catalog.snapshot(self.catalog.current_version)

        result = self.cache.lookup(TABLE, PATH, STMT, snap)
        self.assertTrue(result.rebound)
        self.assertEqual(result.reason, REBIND_DROPPED)
        self.assertEqual(result.oid, replacement.relation_oid)
        self.assertEqual(result.oid, 3)
        # Both on-path drops are relevant events; the reason is still
        # REBIND_DROPPED because the previously bound oid (1) is among them.
        drop_versions = [
            e.version for e in result.events if e.kind == "drop_table"
        ]
        self.assertEqual(drop_versions, [shadow_drop.version, drop.version])
        self.assertTrue(
            any(e.relation_oid == 1 for e in result.events)
        )

    def test_dropped_without_replacement_raises_with_drop_event(self) -> None:
        self.cache.lookup(
            TABLE, PATH, STMT,
            self.catalog.snapshot(self.catalog.current_version),
        )
        drop = self.catalog.drop_table("shared", "orders")
        snap = self.catalog.snapshot(self.catalog.current_version)
        with self.assertRaises(RelationNotFound) as ctx:
            self.cache.lookup(TABLE, PATH, STMT, snap)
        self.assertIs(ctx.exception.drop_event,
                      next(e for e in self.catalog.events()
                           if e.version == drop.version))

    def test_old_snapshot_still_resolves_dropped_table(self) -> None:
        old_snap = self.catalog.snapshot(self.catalog.current_version)
        self.cache.lookup(TABLE, PATH, STMT, old_snap)
        self.catalog.drop_table("shared", "orders")
        # The old transaction cannot see the drop at all.
        reused = self.cache.lookup(TABLE, PATH, STMT, old_snap)
        self.assertTrue(reused.hit)
        self.assertEqual(reused.oid, 1)


class NoOpRebindTest(unittest.TestCase):
    def test_shadow_created_and_removed_between_lookups_rebinds_same_oid(self) -> None:
        catalog = build_catalog()
        cache = BindingCache()
        cache.lookup(
            TABLE, PATH, STMT,
            catalog.snapshot(catalog.current_version),
        )
        catalog.create_table("app", "orders")
        catalog.drop_table("app", "orders")
        snap = catalog.snapshot(catalog.current_version)

        result = cache.lookup(TABLE, PATH, STMT, snap)
        self.assertTrue(result.rebound)
        self.assertEqual(result.reason, REBIND_RESOLUTION_CHANGED)
        self.assertEqual(result.oid, 1)  # same object, but re-resolved

        # Afterwards the entry is fresh again at the new version.
        follow_up = cache.lookup(TABLE, PATH, STMT, snap)
        self.assertTrue(follow_up.hit)
        self.assertEqual(follow_up.reason, HIT_FRESH)


if __name__ == "__main__":
    unittest.main()
