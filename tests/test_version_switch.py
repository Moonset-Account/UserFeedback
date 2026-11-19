"""Version switching: atomic publication, page-boundary growth, restart signal."""

from __future__ import annotations

import threading
import unittest

from password_screening import (
    LocalTransport,
    ProtocolError,
    ScreeningClient,
    ScreeningService,
    VersionStore,
)
from tests.fixtures import forced_prefix_digest, make_bucket


class VersionSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prefix_length = 4
        self.page_size = 4
        self.prefix = "AAAA"
        self.store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size, retain=4)
        self.service = ScreeningService(self.store)
        self.transport = LocalTransport(self.service)
        self.client = ScreeningClient(self.transport, prefix_length=self.prefix_length)
        self._small_bucket = make_bucket(self.prefix, 5, label="old")
        self._large_bucket = make_bucket(self.prefix, 9, label="new")

    # -- helpers ------------------------------------------------------------
    def _publish_old(self, extra: list[str] | None = None):
        digests = list(self._small_bucket) + list(extra or [])
        return self.store.publish(digests, version_id="v-old")

    def _publish_new(self, extra: list[str] | None = None):
        digests = list(self._large_bucket) + list(extra or [])
        return self.store.publish(digests, version_id="v-new")

    def _versions_requested(self) -> list[str | None]:
        return [call.get("version") for call in self.transport.calls]

    # -- accept: finish the pinned version ----------------------------------
    def test_traversal_started_before_update_finishes_on_old_version(self) -> None:
        old = self._publish_old()
        self.assertEqual(old.total_pages, 2)

        def update_after_first_page(page_index, response):
            if page_index == 0 and self.store.current_version_id == "v-old":
                self._publish_new()

        # The record only exists on page 1 of the old version.
        target = self._small_bucket[4]
        result = self.client.screen_digest(target, on_page=update_after_first_page)

        self.assertTrue(result.matched)
        self.assertEqual(result.version_id, "v-old")
        self.assertEqual(result.pages_read, 2)
        self.assertEqual(result.restarts, 0)
        # The first request is unpinned (it discovers the version); every later
        # page is pinned to the version the traversal started on.
        self.assertIsNone(self._versions_requested()[0])
        self.assertEqual(self._versions_requested()[1:], ["v-old"])
        # And the new version is live for the next caller.
        self.assertEqual(self.store.current_version_id, "v-new")

    def test_old_version_answer_is_unaffected_by_new_page_count(self) -> None:
        old = self._publish_old()
        new_bucket = self._large_bucket
        # This digest lives on page 2 of the new version only.
        only_in_new = new_bucket[8]
        self.assertEqual(old.total_pages, 2)

        def update_after_first_page(page_index, response):
            if page_index == 0:
                self._publish_new()

        result = self.client.screen_digest(only_in_new, on_page=update_after_first_page)
        self.assertFalse(result.matched)
        self.assertEqual(result.version_id, "v-old")
        self.assertNotIn("v-new", self._versions_requested())

        # Without a pin, the same digest would be found in the new version.
        fresh = self.client.screen_digest(only_in_new)
        self.assertTrue(fresh.matched)
        self.assertEqual(fresh.version_id, "v-new")

    # -- reject: never mix two versions -------------------------------------
    def test_client_rejects_pages_served_from_different_versions(self) -> None:
        old = self._publish_old()
        self._publish_new()

        class MixingTransport:
            def __init__(self) -> None:
                self.page = 0

            def request(self, payload):
                # Simulates a buggy proxy: page 0 from the old version, page 1
                # from the new one, both claiming success.
                if payload["page"] == 0:
                    version = old
                else:
                    version = self.store.current
                entries = [entry.to_dict() for entry in version.page(payload["prefix"], payload["page"])]
                return {
                    "ok": True,
                    "served_version": version.version_id,
                    "prefix": payload["prefix"],
                    "page": payload["page"],
                    "page_size": version.page_size,
                    "total_pages": version.total_pages,
                    "entries": entries,
                }

        client = ScreeningClient(MixingTransport(), prefix_length=self.prefix_length)
        with self.assertRaises(ProtocolError):
            client.screen_digest(self._small_bucket[0])

    def test_page_level_mix_is_detected_via_shrinking_version(self) -> None:
        """Old total_pages=2, new total_pages=3: a mix must not yield a verdict."""

        old = self._publish_old()
        self._publish_new()
        target = self._large_bucket[8]  # page 2 of the new version

        class ShrinkingTransport:
            def request(self, payload):
                source = old if payload["page"] == 0 else self_store.current
                entries = [entry.to_dict() for entry in source.page(payload["prefix"], payload["page"])]
                return {
                    "ok": True,
                    "served_version": source.version_id,
                    "prefix": payload["prefix"],
                    "page": payload["page"],
                    "page_size": source.page_size,
                    "total_pages": source.total_pages,
                    "entries": entries,
                }

        self_store = self.store
        client = ScreeningClient(ShrinkingTransport(), prefix_length=self.prefix_length)
        with self.assertRaises(ProtocolError):
            client.screen_digest(target)

    # -- explicit restart ---------------------------------------------------
    def test_evicted_version_forces_an_explicit_restart(self) -> None:
        store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size, retain=2)
        service = ScreeningService(store)
        transport = LocalTransport(service)
        client = ScreeningClient(transport, prefix_length=self.prefix_length)

        old = store.publish(self._small_bucket, version_id="v1")
        self.assertEqual(old.total_pages, 2)
        # v3 contains the target; v2 does not.
        target = self._large_bucket[0]

        updated = False

        def update_twice(page_index, response):
            nonlocal updated
            if page_index != 0 or updated:
                return
            updated = True
            store.publish([forced_prefix_digest("BBBB", "x")], version_id="v2")
            store.publish(list(self._large_bucket), version_id="v3")

        result = client.screen_digest(target, on_page=update_twice)
        self.assertTrue(result.matched)
        self.assertEqual(result.version_id, "v3")
        self.assertEqual(result.restarts, 1)
        # Traversal 1 discovers v1 unpinned, then pins it; traversal 2 pins the
        # version it restarted on for every page.
        versions = [call.get("version") for call in transport.calls]
        self.assertIsNone(versions[0])
        self.assertEqual(versions[1], "v1")  # rejected with restart_required
        self.assertTrue(all(v == "v3" for v in versions[2:]))

    def test_service_returns_machine_readable_restart_signal(self) -> None:
        store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size, retain=2)
        service = ScreeningService(store)
        store.publish(self._small_bucket, version_id="v1")
        store.publish(list(self._large_bucket), version_id="v2")
        store.publish([forced_prefix_digest("BBBB", "y")], version_id="v3")

        response = service.handle(
            {"op": "page", "prefix": self.prefix, "page": 1, "version": "v1"}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "restart_required")
        self.assertTrue(response["restart_required"])
        self.assertEqual(response["current_version"], "v3")

    def test_current_version_has_no_restart_signal(self) -> None:
        store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size)
        service = ScreeningService(store)
        published = store.publish(self._small_bucket, version_id="v1")
        for page in range(published.total_pages):
            response = service.handle(
                {"op": "page", "prefix": self.prefix, "page": page, "version": "v1"}
            )
            self.assertTrue(response["ok"])
            self.assertNotIn("restart_required", response)

    # -- atomic publication under concurrency -------------------------------
    def test_concurrent_readers_never_see_a_mixed_version(self) -> None:
        # The shared digest lives in every published version, but the page
        # layout differs (2 pages vs 3).  A reader that mixed versions could
        # read a stale page count, lose the hit, or see a page-size mismatch.
        shared = self._small_bucket[0]
        retain = 200
        store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size, retain=retain)
        service = ScreeningService(store)
        v1_digests = list(self._small_bucket)
        v2_digests = list(self._large_bucket)
        v2_digests[0] = shared
        store.publish(v1_digests, version_id="v1")
        store.publish(v2_digests, version_id="v2")

        errors: list[BaseException] = []

        def publisher() -> None:
            # Bounded below `retain`, so no reader's pinned version is ever
            # evicted and the only failure mode left is a genuine version mix.
            for toggle in range(retain - 4):
                try:
                    if toggle % 2 == 0:
                        store.publish(v1_digests, version_id=f"p-v1-{toggle}")
                    else:
                        store.publish(v2_digests, version_id=f"p-v2-{toggle}")
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

        def reader() -> None:
            client = ScreeningClient(LocalTransport(service), prefix_length=self.prefix_length)
            for _ in range(20):
                try:
                    result = client.screen_digest(shared)
                    if not result.matched:
                        errors.append(AssertionError(f"lost a hit in {result.version_id}"))
                except BaseException as exc:  # pragma: no cover - failure path
                    errors.append(exc)

        publisher_thread = threading.Thread(target=publisher)
        readers = [threading.Thread(target=reader) for _ in range(4)]
        publisher_thread.start()
        for thread in readers:
            thread.start()
        publisher_thread.join()
        for thread in readers:
            thread.join()

        self.assertEqual(errors, [])
        # Sanity: the publication actually raced with the readers.
        self.assertGreaterEqual(len(store.retained_versions()), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
