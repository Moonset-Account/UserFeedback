"""Tests for the sanitizing exporter, using synthetic blocks that carry
non-zero historical tails (simulating deleted-file remnants in free space).
"""

import unittest

from sanitized_export import (
    ExportInterrupted,
    Extent,
    InMemoryBlockDevice,
    SanitizingExporter,
    SparseRegion,
    VolumeView,
)

BS = 8  # block size used throughout the tests


def make_device(blocks: dict[int, bytes]) -> InMemoryBlockDevice:
    dev = InMemoryBlockDevice(BS)
    for bid, data in blocks.items():
        dev.write_block(bid, data)
    return dev


class SanitizingExportTest(unittest.TestCase):
    def test_allocated_valid_bytes_preserved(self):
        dev = make_device({1: b"ABCDEFGH", 2: b"IJKLMNOP"})
        view = VolumeView(
            name="v1",
            block_count=2,
            valid_bytes=2 * BS,
            extents=(Extent(0, 1, 2),),
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 0, 2 * BS), b"ABCDEFGHIJKLMNOP")

    def test_historical_nonzero_tail_is_zeroed(self):
        # Physical block holds 3 valid bytes plus a non-zero historical tail.
        dev = make_device({1: b"abcXXXXX"})
        view = VolumeView(
            name="v1",
            block_count=1,
            valid_bytes=3,
            extents=(Extent(0, 1, 1),),
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 0, BS), b"abc" + bytes(5))
        # The stored sanitized block itself must be scrubbed, not just reads.
        (bid,) = snap.reachable_blocks("v1")
        self.assertEqual(snap.read_block(bid), b"abc" + bytes(5))

    def test_sparse_region_reads_zero(self):
        dev = make_device({1: b"ABCDEFGH"})
        view = VolumeView(
            name="v1",
            block_count=3,
            valid_bytes=3 * BS,
            extents=(Extent(0, 1, 1),),
            sparse=(SparseRegion(1, 1),),
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 0, 3 * BS), b"ABCDEFGH" + bytes(16))

    def test_unallocated_hole_reads_zero(self):
        dev = make_device({1: b"ABCDEFGH"})
        view = VolumeView(
            name="v1",
            block_count=2,
            valid_bytes=2 * BS,
            extents=(Extent(0, 1, 1),),
            # logical block 1 is a hole: no extent, no sparse entry
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 0, 2 * BS), b"ABCDEFGH" + bytes(8))

    def test_shared_block_with_different_valid_tail_bounds(self):
        # One physical block shared by two views (snapshot semantics); each
        # view declares a different valid tail bound inside that block.
        dev = make_device({7: b"DATAh1st"})  # 4 valid bytes + historical tail
        view_a = VolumeView(
            name="full",
            block_count=1,
            valid_bytes=BS,
            extents=(Extent(0, 7, 1),),
        )
        view_b = VolumeView(
            name="trimmed",
            block_count=1,
            valid_bytes=4,
            extents=(Extent(0, 7, 1),),
        )
        snap = SanitizingExporter(dev).export([view_a, view_b])
        self.assertEqual(snap.read("full", 0, BS), b"DATAh1st")
        self.assertEqual(snap.read("trimmed", 0, BS), b"DATA" + bytes(4))
        # Different sanitized content -> different exported blocks.
        self.assertNotEqual(
            snap.block_table("full")[0], snap.block_table("trimmed")[0]
        )
        self.assertEqual(len(snap.stored_blocks()), 2)

    def test_shared_block_with_same_tail_bound_is_reused(self):
        dev = make_device({7: b"SHARED!!"})
        view_a = VolumeView(
            name="a", block_count=1, valid_bytes=BS, extents=(Extent(0, 7, 1),)
        )
        view_b = VolumeView(
            name="b", block_count=1, valid_bytes=BS, extents=(Extent(0, 7, 1),)
        )
        snap = SanitizingExporter(dev).export([view_a, view_b])
        self.assertEqual(
            snap.block_table("a")[0], snap.block_table("b")[0]
        )
        self.assertEqual(len(snap.stored_blocks()), 1)

    def test_content_based_reuse_and_single_zero_block(self):
        dev = make_device({1: b"SAMESAME", 2: b"SAMESAME", 3: b"OTHER!!!"})
        view = VolumeView(
            name="v1",
            block_count=5,
            valid_bytes=3 * BS,
            extents=(Extent(0, 1, 3),),
            # blocks 3, 4: holes beyond valid_bytes -> zero
        )
        snap = SanitizingExporter(dev).export([view])
        table = snap.block_table("v1")
        # Identical sanitized content reuses one block.
        self.assertEqual(table[0], table[1])
        self.assertNotEqual(table[0], table[2])
        # All zero blocks (holes / past-valid) collapse to a single block.
        self.assertEqual(table[3], table[4])
        self.assertEqual(snap.read_block(table[3]), bytes(BS))
        # Stored set is exactly {same, other, zero}.
        self.assertEqual(len(snap.stored_blocks()), 3)

    def test_zero_valid_bytes_reads_all_zero(self):
        dev = make_device({1: b"SECRET!!"})
        view = VolumeView(
            name="v1",
            block_count=2,
            valid_bytes=0,
            extents=(Extent(0, 1, 1),),
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 0, 2 * BS), bytes(2 * BS))
        self.assertEqual(snap.stored_blocks(), snap.reachable_blocks("v1"))
        self.assertEqual(len(snap.stored_blocks()), 1)  # just the zero block

    def test_valid_bytes_exact_block_boundary_keeps_full_block(self):
        dev = make_device({1: b"BLOCKONE", 2: b"BLOCKTWO"})
        view = VolumeView(
            name="v1",
            block_count=2,
            valid_bytes=BS,  # block 0 fully valid, block 1 fully invalid
            extents=(Extent(0, 1, 2),),
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 0, 2 * BS), b"BLOCKONE" + bytes(8))

    def test_interleaved_extents_sparse_and_holes(self):
        # Layout: extent | sparse | extent | hole | extent(partial valid)
        dev = make_device(
            {1: b"AAAAAAAA", 2: b"BBBBBBBB", 3: b"CCCCCccc"}
        )
        view = VolumeView(
            name="mix",
            block_count=5,
            valid_bytes=2 * BS + 5,  # valid through 5 bytes of block 2
            extents=(
                Extent(0, 1, 1),
                Extent(2, 2, 1),
                Extent(4, 3, 1),
            ),
            sparse=(SparseRegion(1, 1),),
            # logical block 3: hole
        )
        snap = SanitizingExporter(dev).export([view])
        expected = (
            b"AAAAAAAA"      # extent, fully valid
            + bytes(8)        # sparse
            + b"BBBBB" + bytes(3)  # extent, partial valid tail scrubbed
            + bytes(8)        # hole
            + bytes(8)        # extent but entirely past valid_bytes
        )
        self.assertEqual(snap.read("mix", 0, 5 * BS), expected)

    def test_read_offsets_lengths_and_clamping(self):
        dev = make_device({1: b"ABCDEFGH", 2: b"IJKLMNOP"})
        view = VolumeView(
            name="v1", block_count=2, valid_bytes=2 * BS,
            extents=(Extent(0, 1, 2),),
        )
        snap = SanitizingExporter(dev).export([view])
        self.assertEqual(snap.read("v1", 3, 7), b"DEFGHIJ")  # crosses blocks
        self.assertEqual(snap.read("v1", 14, 100), b"OP")    # clamped to end
        self.assertEqual(snap.read("v1", 16, 5), b"")        # at end
        self.assertEqual(snap.read("v1", 0, 0), b"")         # empty read
        with self.assertRaises(ValueError):
            snap.read("v1", -1, 4)
        with self.assertRaises(KeyError):
            snap.read("nope", 0, 1)

    def test_reachable_block_set_matches_stored_blocks(self):
        dev = make_device(
            {1: b"KEEPthis", 2: b"GONEgone", 9: b"ORPHANED"}  # 9: unreferenced
        )
        view = VolumeView(
            name="v1",
            block_count=3,
            valid_bytes=4,
            extents=(Extent(0, 1, 1), Extent(1, 2, 1), Extent(2, 9, 1)),
        )
        snap = SanitizingExporter(dev).export([view])
        # Stored set == reachable set: no unreachable garbage is carried
        # into the export and nothing reachable is missing.
        self.assertEqual(snap.stored_blocks(), snap.reachable_blocks())
        # No reachable block contains non-zero bytes beyond its view's
        # valid tail bound (valid_bytes=4 -> only 4 valid bytes overall).
        for bid in snap.reachable_blocks():
            block = snap.read_block(bid)
            if block != bytes(BS):
                self.assertEqual(block, b"KEEP" + bytes(4))

    def test_interrupted_export_publishes_nothing_then_full_retry(self):
        dev = make_device({1: b"AAAAAAAA", 2: b"BBBBBBBB", 3: b"CCCCCCCC"})
        views = [
            VolumeView(
                name="v1", block_count=2, valid_bytes=2 * BS,
                extents=(Extent(0, 1, 2),),
            ),
            VolumeView(
                name="v2", block_count=2, valid_bytes=BS,
                extents=(Extent(0, 3, 1), Extent(1, 1, 1)),
            ),
        ]
        exporter = SanitizingExporter(dev)
        before = exporter.snapshot
        exporter.interrupt_after = 3  # crash mid-way through view v2
        with self.assertRaises(ExportInterrupted):
            exporter.export(views)
        # Published state is byte-for-byte the old one.
        self.assertIs(exporter.snapshot, before)
        self.assertEqual(exporter.snapshot.view_names(), ())
        # Restart: publishes a complete, consistent new block table set.
        exporter.interrupt_after = None
        snap = exporter.export(views)
        self.assertEqual(snap.view_names(), ("v1", "v2"))
        for name in snap.view_names():
            table = snap.block_table(name)
            self.assertEqual(sorted(table), [0, 1])  # full table, no gaps
        self.assertEqual(snap.stored_blocks(), snap.reachable_blocks())
        self.assertEqual(snap.read("v1", 0, 2 * BS), b"AAAAAAAABBBBBBBB")
        self.assertEqual(snap.read("v2", 0, 2 * BS), b"CCCCCCCC" + bytes(8))

    def test_second_export_replaces_snapshot_atomically(self):
        dev = make_device({1: b"FIRST!!!", 2: b"SECOND!!"})
        exporter = SanitizingExporter(dev)
        v1 = VolumeView(
            name="v", block_count=1, valid_bytes=BS, extents=(Extent(0, 1, 1),)
        )
        snap1 = exporter.export([v1])
        v2 = VolumeView(
            name="v", block_count=1, valid_bytes=BS, extents=(Extent(0, 2, 1),)
        )
        exporter.interrupt_after = 1
        with self.assertRaises(ExportInterrupted):
            exporter.export([v2])
        self.assertIs(exporter.snapshot, snap1)  # old snapshot survives
        exporter.interrupt_after = None
        snap2 = exporter.export([v2])
        self.assertEqual(snap2.read("v", 0, BS), b"SECOND!!")
        self.assertEqual(snap1.read("v", 0, BS), b"FIRST!!!")  # old intact

    def test_validation_rejects_bad_views(self):
        dev = make_device({1: b"XXXXXXXX"})
        base = dict(name="v", block_count=2, valid_bytes=BS,
                    extents=(Extent(0, 1, 1),))
        cases = [
            VolumeView(**{**base, "valid_bytes": 3 * BS}),      # too large
            VolumeView(**{**base, "valid_bytes": -1}),          # negative
            VolumeView(**{**base, "extents": (Extent(1, 1, 5),)}),  # past end
            VolumeView(  # overlapping extents
                **{**base, "extents": (Extent(0, 1, 2), Extent(1, 1, 1))}
            ),
            VolumeView(  # extent overlapping sparse region
                **{**base, "sparse": (SparseRegion(0, 1),)}
            ),
            VolumeView(**{**base, "sparse": (SparseRegion(2, 1),)}),  # past end
        ]
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SanitizingExporter(dev).export([bad])
        # Duplicate view names are rejected too.
        good = VolumeView(**base)
        with self.assertRaises(ValueError):
            SanitizingExporter(dev).export([good, good])
        # Failed validation must not disturb the published snapshot.
        self.assertEqual(
            SanitizingExporter(dev).snapshot.view_names(), ()
        )

    def test_device_read_of_unwritten_block_is_zero(self):
        dev = InMemoryBlockDevice(BS)
        self.assertEqual(dev.read_block(42), bytes(BS))
        with self.assertRaises(ValueError):
            dev.write_block(1, b"short")


if __name__ == "__main__":
    unittest.main()
