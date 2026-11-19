"""Boundary case: an update pushes the fullest bucket across a page boundary.

The version's page count is derived from the fullest bucket, so adding one
record can change ``total_pages`` for every prefix.  An in-flight traversal
must either finish the pinned version or get the explicit restart signal - it
must never observe a page count / page size that belongs to two versions.
"""

from __future__ import annotations

import unittest

from password_screening import (
    LocalTransport,
    ProtocolError,
    ScreeningClient,
    ScreeningService,
    VersionStore,
)
from password_screening.version import PADDING, RECORD
from tests.fixtures import forced_prefix_digest, make_bucket


class PageBoundaryCrossingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.page_size = 4
        self.prefix_length = 4
        self.prefix = "BEEF"
        self.store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size, retain=4)
        self.service = ScreeningService(self.store)
        self.transport = LocalTransport(self.service)

    def test_exact_page_fill_then_one_more_record_doubles_the_page_count(self) -> None:
        exact = make_bucket(self.prefix, self.page_size, label="exact")
        before = self.store.publish(exact, version_id="v-exact")
        self.assertEqual(before.total_pages, 1)

        after = self.store.publish(
            exact + make_bucket(self.prefix, self.page_size + 1, label="grown")[:1],
            version_id="v-grown",
        )
        self.assertEqual(after.total_pages, 2)

        # The new last page is short but still padded to the full page size.
        entries = after.page(self.prefix, 1)
        self.assertEqual(len(entries), self.page_size)
        self.assertEqual(sum(1 for entry in entries if entry.kind == RECORD), 1)
        self.assertEqual(sum(1 for entry in entries if entry.kind == PADDING), self.page_size - 1)

        # Unrelated prefixes now also advertise two pages.
        empty = before.page("0000", 0)
        self.assertEqual(len(empty), self.page_size)
        self.assertEqual(self.service.handle({"op": "page", "prefix": "0000", "page": 0})["total_pages"], 2)

    def test_inflight_traversal_uses_old_page_count_when_update_crosses_boundary(self) -> None:
        exact = make_bucket(self.prefix, self.page_size, label="exact")
        self.store.publish(exact, version_id="v-exact")
        # The 5th record in the *same* prefix bucket is what pushes it over.
        dedicated = make_bucket(self.prefix, self.page_size + 1, label="grown")[self.page_size]
        client = ScreeningClient(self.transport, prefix_length=self.prefix_length)

        def grow(page_index, response) -> None:
            if page_index == 0:
                # Cross the boundary *after* the client has page 0 of v-exact.
                self.store.publish(
                    list(exact) + [dedicated], version_id="v-grown"
                )

        result = client.screen_digest(dedicated, on_page=grow)
        # v-exact has one page, so the traversal legitimately ends without the
        # new record: no mixed verdict, no silent hit.
        self.assertEqual(result.version_id, "v-exact")
        self.assertEqual(result.pages_read, 1)
        self.assertFalse(result.matched)
        self.assertEqual(result.restarts, 0)

        # A fresh traversal sees the grown version and finds it.
        fresh = client.screen_digest(dedicated)
        self.assertTrue(fresh.matched)
        self.assertEqual(fresh.version_id, "v-grown")
        self.assertEqual(fresh.pages_read, 2)

    def test_client_never_accepts_a_page_count_that_changes_mid_traversal(self) -> None:
        exact = make_bucket(self.prefix, self.page_size, label="exact")
        self.store.publish(exact, version_id="v-exact")
        grown = self.store.publish(list(exact) + [forced_prefix_digest("CAFE", "x")], version_id="v-grown")

        class BoundaryRacingTransport:
            """Page 0 reports the grown layout, page 1 the pre-grow layout."""

            def request(self, payload):
                page = payload["page"]
                if page == 0:
                    return {
                        "ok": True,
                        "served_version": "v-grown",
                        "prefix": payload["prefix"],
                        "page": 0,
                        "page_size": self.page_size,
                        "total_pages": 2,
                        "entries": [entry.to_dict() for entry in grown.page(payload["prefix"], 0)],
                    }
                return {
                    "ok": True,
                    "served_version": "v-grown",
                    "prefix": payload["prefix"],
                    "page": 1,
                    "page_size": self.page_size,
                    "total_pages": 1,  # contradicts page 0
                    "entries": [entry.to_dict() for entry in exact.page(payload["prefix"], 0)],
                }

            page_size = self.page_size

        client = ScreeningClient(BoundaryRacingTransport(), prefix_length=self.prefix_length)
        with self.assertRaises(ProtocolError):
            client.screen_digest(exact[0])

    def test_service_refuses_a_stale_page_number_after_shrinkage(self) -> None:
        big = make_bucket(self.prefix, 9, label="big")
        self.store.publish(big, version_id="v-big")  # 3 pages
        self.store.publish(big[:2], version_id="v-small")  # 1 page

        stale = self.service.handle(
            {"op": "page", "prefix": self.prefix, "page": 2, "version": "v-big"}
        )
        # v-big is still retained, so its 3-page layout keeps serving correctly.
        self.assertTrue(stale["ok"])
        self.assertEqual(stale["total_pages"], 3)

        # Once v-big is retired, page 2 is not quietly served from v-small.
        for index in range(3):  # retain=4: v-big falls out after these publishes
            self.store.publish(
                [forced_prefix_digest("DEAD", f"filler-{index}")], version_id=f"v-filler-{index}"
            )
        retired = self.service.handle(
            {"op": "page", "prefix": self.prefix, "page": 2, "version": "v-big"}
        )
        self.assertFalse(retired["ok"])
        self.assertEqual(retired["error"], "restart_required")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
