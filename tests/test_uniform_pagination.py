"""Uniform pagination contract: every prefix, including empty buckets, looks alike."""

from __future__ import annotations

import unittest

from password_screening import (
    DigestError,
    InvalidPrefix,
    LocalTransport,
    PageOutOfRange,
    ProtocolError,
    ScreeningClient,
    ScreeningService,
    VersionStore,
    build_version,
)
from password_screening.version import PADDING, RECORD
from tests.fixtures import digest_of, forced_prefix_digest, make_bucket


class UniformPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prefix_length = 4
        self.page_size = 4
        # 3 records in one bucket (fits page 0 with one pad), 6 in another
        # (page 0 full, page 1 half full).
        self.small_prefix = "AAAA"
        self.large_prefix = "BBBB"
        self.small_bucket = make_bucket(self.small_prefix, 3, label="small")
        self.large_bucket = make_bucket(self.large_prefix, 6, label="large")
        self.version = build_version(
            self.small_bucket + self.large_bucket,
            prefix_length=self.prefix_length,
            page_size=self.page_size,
            version_id="v-uniform",
        )

    def test_fullest_bucket_sets_page_count_for_every_prefix(self) -> None:
        # 6 records / 4 per page -> 2 pages, even for the 3-record and empty buckets.
        self.assertEqual(self.version.total_pages, 2)
        for prefix in (self.small_prefix, self.large_prefix, "CCCC", "0123"):
            for page in range(self.version.total_pages):
                entries = self.version.page(prefix, page)
                self.assertEqual(len(entries), self.page_size, (prefix, page))

    def test_empty_bucket_returns_only_typed_padding(self) -> None:
        entries = self.version.page("CCCC", 0)
        self.assertEqual(len(entries), self.page_size)
        self.assertTrue(all(entry.kind == PADDING for entry in entries))
        self.assertEqual(len({entry.suffix for entry in entries}), self.page_size)

    def test_record_and_padding_kinds_are_distinguishable(self) -> None:
        entries = self.version.page(self.small_prefix, 0)
        kinds = [entry.kind for entry in entries]
        self.assertEqual(kinds.count(RECORD), 3)
        self.assertEqual(kinds.count(PADDING), 1)
        self.assertTrue(set(kinds) <= {RECORD, PADDING})

    def test_padding_suffixes_never_collide_with_real_suffixes(self) -> None:
        real_suffixes = {digest[self.prefix_length :] for digest in self.small_bucket}
        padding = [
            entry.suffix
            for entry in self.version.page(self.small_prefix, 1)
            if entry.kind == PADDING
        ]
        self.assertTrue(padding)
        self.assertFalse(real_suffixes & set(padding))
        # And they are not real across the whole version either.
        for suffix in padding:
            self.assertFalse(self.version.has_digest(self.small_prefix + suffix))

    def test_page_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(PageOutOfRange):
            self.version.page(self.large_prefix, self.version.total_pages)
        with self.assertRaises(InvalidPrefix):
            self.version.page("XY", 0)

    def test_impossible_page_size_for_the_suffix_space_is_rejected(self) -> None:
        # prefix_length=63 leaves a 1-hex suffix space of 16 values, which can
        # never fill a 1000-entry page.
        with self.assertRaises(DigestError):
            build_version([], prefix_length=63, page_size=1000)

    def test_single_empty_version_still_serves_one_page(self) -> None:
        empty = build_version([], prefix_length=4, page_size=4, version_id="v-empty")
        self.assertEqual(empty.total_pages, 1)
        self.assertEqual(len(empty.page("FFFF", 0)), 4)

    def test_padded_bucket_never_reports_a_hit_on_its_padding(self) -> None:
        store = VersionStore(prefix_length=self.prefix_length, page_size=self.page_size)
        store.publish_version(self.version)
        client = ScreeningClient(
            LocalTransport(ScreeningService(store)), prefix_length=self.prefix_length
        )
        # Every real record of the 3-record bucket is a hit...
        for digest in self.small_bucket:
            self.assertTrue(client.screen_digest(digest).matched)
        # ...and no padding suffix in any page of that bucket is.
        seen = set()
        for page in range(self.version.total_pages):
            for entry in self.version.page(self.small_prefix, page):
                if entry.kind == PADDING:
                    seen.add(entry.suffix)
        self.assertTrue(seen)
        for suffix in seen:
            result = client.screen_digest(self.small_prefix + suffix)
            self.assertFalse(result.matched, suffix)

    def test_prefix_length_and_page_size_are_consistent_in_info(self) -> None:
        self.assertEqual(self.version.prefix_length, self.prefix_length)
        self.assertEqual(self.version.page_size, self.page_size)
        info = self.version.info()
        self.assertEqual(info["total_pages"], 2)
        self.assertEqual(info["digest_count"], 9)


class ClientUniformityTests(unittest.TestCase):
    """The client must see identical shapes for hit, miss and empty buckets."""

    def setUp(self) -> None:
        self.store = VersionStore(prefix_length=4, page_size=3)
        self.hit_prefix = "DDDD"
        self.records = make_bucket(self.hit_prefix, 2, label="hit")
        self.store.publish(self.records, version_id="v-shapes")
        self.transport = LocalTransport(ScreeningService(self.store))
        self.client = ScreeningClient(self.transport, prefix_length=4)

    def test_hit_miss_and_empty_bucket_share_shape(self) -> None:
        observed = {}

        for label, digest in (
            ("hit", self.records[0]),
            ("miss", forced_prefix_digest("DDDD", "not-in-list")),
            ("empty", forced_prefix_digest("EEEE", "empty-bucket")),
        ):
            pages = []

            def capture(page_index, response, pages=pages):
                pages.append(response)

            result = self.client.screen_digest(digest, on_page=capture)
            observed[label] = (
                [len(p["entries"]) for p in pages],
                {p["page_size"] for p in pages},
                {p["total_pages"] for p in pages},
                result.pages_read,
            )

        self.assertEqual(observed["hit"][0], observed["miss"][0])
        self.assertEqual(observed["hit"][0], observed["empty"][0])
        self.assertEqual(observed["hit"][2], observed["empty"][2])
        self.assertEqual(observed["hit"][3], observed["empty"][3])
        self.assertEqual(observed["hit"][1], {3})

    def test_empty_bucket_is_not_a_hit_even_if_padding_matches_suffix(self) -> None:
        """A colluding/buggy server cannot turn padding into a real hit."""

        target = forced_prefix_digest("EEEE", "padding-target")
        suffix = target[4:]

        class MaliciousTransport:
            def __init__(self) -> None:
                self.calls = 0

            def request(self, payload):
                self.calls += 1
                return {
                    "ok": True,
                    "op": "page",
                    "served_version": "v-shapes",
                    "prefix": payload["prefix"],
                    "page": 0,
                    "page_size": 3,
                    "total_pages": 1,
                    "entries": [
                        {"kind": PADDING, "suffix": suffix},
                        {"kind": PADDING, "suffix": "0" * 60},
                        {"kind": PADDING, "suffix": "1" * 60},
                    ],
                }

        malicious = ScreeningClient(MaliciousTransport(), prefix_length=4)
        self.assertFalse(malicious.screen_digest(target).matched)

    def test_client_rejects_malformed_suffix(self) -> None:
        class BadSuffixTransport:
            def request(self, payload):
                return {
                    "ok": True,
                    "served_version": "v",
                    "prefix": payload["prefix"],
                    "page": 0,
                    "page_size": 1,
                    "total_pages": 1,
                    "entries": [{"kind": RECORD, "suffix": "zz"}],
                }

        client = ScreeningClient(BadSuffixTransport(), prefix_length=4)
        with self.assertRaises(ProtocolError):
            client.screen_digest(digest_of("anything"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
