"""End-to-end tests through Session/Transaction/PreparedStatement."""

import unittest

from plan_cache import (
    BindingCache,
    Catalog,
    HIT_FRESH,
    HIT_NO_RELEVANT_CHANGE,
    NamespaceNotFound,
    REBIND_FIRST_USE,
    REBIND_SHADOWED,
    SEARCH_PATH_CHANGED,
    Session,
)

STMT = "SELECT * FROM orders WHERE id = $1"
TABLE = "orders"


class SessionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = Catalog()
        self.catalog.create_namespace("app")
        self.catalog.create_namespace("shared")
        self.catalog.create_namespace("misc")
        self.catalog.create_table("shared", "orders")  # oid 1
        self.cache = BindingCache()
        self.session = Session(
            self.catalog, self.cache, search_path=("app", "shared")
        )


class TransactionPinningTest(SessionFixture):
    def test_transaction_pins_catalog_version_seen_at_begin(self) -> None:
        self.session.begin()
        pinned = self.session.transaction.snapshot_version
        stmt = self.session.prepare(STMT, TABLE)
        first = self.session.execute(stmt)
        self.assertEqual((first.oid, first.reason), (1, REBIND_FIRST_USE))

        # DDL while the transaction stays open must not change its view.
        self.catalog.create_table("app", "orders")  # oid 2
        self.assertEqual(self.session.transaction.snapshot_version, pinned)

        repeated = self.session.execute(stmt)
        self.assertTrue(repeated.hit)
        self.assertEqual(repeated.oid, 1)
        self.session.commit()

    def test_new_transaction_follows_the_shadow(self) -> None:
        # First transaction binds the shared table.
        self.session.begin()
        stmt = self.session.prepare(STMT, TABLE)
        self.assertEqual(self.session.execute(stmt).oid, 1)
        self.session.commit()

        self.catalog.create_table("app", "orders")  # oid 2

        # A new transaction sees the newer catalog version.
        self.session.begin()
        self.assertGreater(
            self.session.transaction.snapshot_version,
            self.cache.history(TABLE, ("app", "shared"), STMT)[0]
            .catalog_version,
        )
        moved = self.session.execute(stmt)
        self.assertTrue(moved.rebound)
        self.assertEqual(moved.reason, REBIND_SHADOWED)
        self.assertEqual(moved.oid, 2)
        self.session.commit()

    def test_interleaved_long_running_and_new_transactions(self) -> None:
        # Long-running transaction T1 binds the original object and stays open.
        self.session.begin()
        t1 = self.session.transaction
        stmt = self.session.prepare(STMT, TABLE)
        self.assertEqual(self.session.execute(stmt).oid, 1)

        # DDL happens; T1 keeps its pinned snapshot.
        shadow_event = self.catalog.create_table("app", "orders")
        self.assertEqual(t1.snapshot_version, shadow_event.version - 1)

        # A second, short transaction in another session on the shared cache
        # binds the shadow and commits.
        other = Session(
            self.catalog, self.cache, search_path=("app", "shared")
        )
        other.begin()
        stmt_copy = other.prepare(STMT, TABLE)
        t2_result = other.execute(stmt_copy)
        self.assertTrue(t2_result.rebound)
        self.assertEqual(t2_result.reason, REBIND_SHADOWED)
        self.assertEqual(t2_result.oid, shadow_event.relation_oid)
        other.commit()

        # Back in T1: still the old object, served as a cache hit.
        t1_again = self.session.execute(stmt)
        self.assertTrue(t1_again.hit)
        self.assertEqual(t1_again.oid, 1)

        # T1 ends; the next transaction starts at the newest version and hits
        # the shadow binding that T2 populated.
        self.session.commit()
        self.session.begin()
        newest = self.session.execute(stmt)
        self.assertTrue(newest.hit)
        self.assertEqual(newest.oid, shadow_event.relation_oid)
        self.session.commit()

        history = self.cache.history(TABLE, ("app", "shared"), STMT)
        self.assertEqual([b.oid for b in history], [1, 2])

    def test_off_path_ddl_lets_the_prepared_plan_continue(self) -> None:
        self.session.begin()
        stmt = self.session.prepare(STMT, TABLE)
        self.assertEqual(self.session.execute(stmt).oid, 1)
        self.session.commit()

        self.catalog.create_table("misc", "orders")
        self.catalog.create_table("misc", "audit")

        self.session.begin()
        result = self.session.execute(stmt)
        self.assertTrue(result.hit)
        self.assertEqual(result.reason, HIT_NO_RELEVANT_CHANGE)
        self.assertEqual(result.oid, 1)
        self.session.commit()


class SearchPathTest(SessionFixture):
    def test_change_path_rebinds_through_a_different_cache_key(self) -> None:
        self.session.begin()
        stmt = self.session.prepare(STMT, TABLE)
        self.assertEqual(self.session.execute(stmt).oid, 1)
        self.session.commit()

        self.catalog.create_table("app", "orders")  # oid 2

        # Same statement, path reordered: first the shared namespace wins.
        self.session.begin()
        self.session.set_search_path("shared", "app")
        reordered = self.session.execute(stmt)
        self.assertTrue(reordered.search_path_changed)
        self.assertTrue(reordered.rebound)
        self.assertEqual(
            reordered.reason, f"{REBIND_FIRST_USE}+{SEARCH_PATH_CHANGED}"
        )
        self.assertEqual(reordered.oid, 1)

        # Switching back to the original path now hits the shadow binding.
        self.session.set_search_path("app", "shared")
        shadowed = self.session.execute(stmt)
        self.assertTrue(shadowed.search_path_changed)
        self.assertEqual(shadowed.oid, 2)
        self.session.commit()

        # Cache entries are independent per search path.
        self.assertEqual(
            len(self.cache.history(TABLE, ("shared", "app"), STMT)), 1
        )
        self.assertEqual(
            len(self.cache.history(TABLE, ("app", "shared"), STMT)), 2
        )

    def test_setting_unknown_namespace_is_rejected(self) -> None:
        with self.assertRaises(NamespaceNotFound):
            self.session.set_search_path("nope")

    def test_execute_requires_an_open_transaction(self) -> None:
        stmt = self.session.prepare(STMT, TABLE)
        with self.assertRaises(RuntimeError):
            self.session.execute(stmt)


class EagerPrepareTest(SessionFixture):
    def test_prepare_bind_now_eagerly_binds_inside_transaction(self) -> None:
        self.session.begin()
        stmt = self.session.prepare(STMT, TABLE, bind_now=True)
        history = self.cache.history(TABLE, ("app", "shared"), STMT)
        self.assertEqual(len(history), 1)
        result = self.session.execute(stmt)
        self.assertTrue(result.hit)
        self.assertEqual(result.reason, HIT_FRESH)
        self.session.commit()


if __name__ == "__main__":
    unittest.main()
