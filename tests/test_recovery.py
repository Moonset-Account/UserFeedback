"""Local synthetic-data tests for shard recovery.

These tests build checkpoints entirely from in-memory matrices on the local
filesystem; no network access or external service is involved.

NOTE: per the task constraints these tests were authored but NOT executed in
this environment.  They are standard ``unittest`` cases and can be run later
with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from typing import Dict, List, Sequence, Tuple

from shard_recovery import (
    HEADER_SIZE,
    ByteRange,
    Rect,
    ShardFile,
    ShardPlan,
    TensorSource,
    build_checkpoint,
    cover_rect,
    load_manifest,
    publish_recovery,
    recover_rank,
    write_shard,
)
from shard_recovery.recovery import (
    ERROR_FORMAT,
    ERROR_MISSING,
    ERROR_OVERLAP,
    ERROR_SHAPE_MISMATCH,
)


# ---------------------------------------------------------------------------
# Small synthetic matrix helpers
# ---------------------------------------------------------------------------


def make_matrix(rows: int, cols: int, *, tag: float) -> List[List[float]]:
    """Deterministic little-endian float32-friendly values (all exactly storable)."""

    return [[float(tag + r * cols + c) for c in range(cols)] for r in range(rows)]


def cut_sources(
    weight: Sequence[Sequence[float]],
    optimizer: Sequence[Sequence[float]],
    plans: Sequence[ShardPlan],
) -> Dict[str, TensorSource]:
    rows = len(weight)
    cols = len(weight[0])
    return {
        "weight": TensorSource(rows, cols, weight, plans),
        "optimizer_m": TensorSource(rows, cols, optimizer, plans),
    }


def slice_matrix(matrix: Sequence[Sequence[float]], rect: Rect) -> Tuple[Tuple[float, ...], ...]:
    return tuple(
        tuple(matrix[r][c] for c in range(rect.col0, rect.col1))
        for r in range(rect.row0, rect.row1)
    )


def assert_ranges_avoid_padding(
    testcase: unittest.TestCase,
    path: str,
    ranges: Sequence[ByteRange],
) -> None:
    """Every accessed range must sit inside a valid float32 segment of a row.

    This directly verifies that row-end padding is skipped, not read.
    """

    with ShardFile(path) as shard:
        header = shard.header
        stride = header.row_stride_bytes
        for accessed in ranges:
            testcase.assertGreaterEqual(accessed.start, HEADER_SIZE)
            local_offset = accessed.start - HEADER_SIZE
            row_index = local_offset // stride
            within_row = local_offset % stride
            testcase.assertLess(row_index, header.valid_rows)
            valid_end = within_row + accessed.length
            testcase.assertLessEqual(
                valid_end,
                header.valid_cols * 4,
                f"range {accessed} crosses row-end padding in {path}",
            )
            # End-of-file padding lives after all physical rows.
            testcase.assertLessEqual(
                accessed.end,
                HEADER_SIZE + header.valid_rows * stride,
                f"range {accessed} reaches end padding in {path}",
            )


class RecoveryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="shard-recovery-test-")
        self.old_root = os.path.join(self.tmp, "old_checkpoint")
        self.new_root = os.path.join(self.tmp, "new_rank")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.old_root, "manifest.json")

    def access_by_suffix(self, recovered) -> Dict[str, Tuple[ByteRange, ...]]:
        return {os.path.basename(a.path): a.accessed_ranges for a in recovered.accessed}


# ---------------------------------------------------------------------------
# 1. Headline scenario: two padded row shards -> one narrow column shard.
# ---------------------------------------------------------------------------


class RowShardsToColumnShardTest(RecoveryTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.rows, self.cols = 6, 8
        self.weight = make_matrix(self.rows, self.cols, tag=0.0)
        self.optimizer = make_matrix(self.rows, self.cols, tag=1000.0)

        # Two row shards, both with a wider physical stride (row-end padding)
        # and the second one additionally padded at end of file for alignment.
        plans = (
            ShardPlan(rect=Rect(0, 0, 3, 8), stride_floats=10),
            ShardPlan(rect=Rect(3, 0, 6, 8), stride_floats=10, end_padding=6),
        )
        build_checkpoint(
            self.old_root,
            cut_sources(self.weight, self.optimizer, plans),
        )

        self.target = Rect(0, 2, 6, 5)  # narrow column shard: all rows, 3 cols

    def test_recovers_weights_and_optimizer_state(self) -> None:
        result = recover_rank(self.manifest_path, target_rank=7, target_rect=self.target)
        self.assertTrue(result.ok, [str(e) for e in result.errors])
        self.assertEqual(len(result.tensors), 2)

        weight = result.tensor("weight")
        optimizer = result.tensor("optimizer_m")
        self.assertEqual((weight.rows, weight.cols), (6, 3))
        self.assertEqual((optimizer.rows, optimizer.cols), (6, 3))
        self.assertEqual(weight.values, slice_matrix(self.weight, self.target))
        self.assertEqual(optimizer.values, slice_matrix(self.optimizer, self.target))

    def test_accessed_byte_ranges_are_exact_and_skip_padding(self) -> None:
        result = recover_rank(self.manifest_path, target_rank=7, target_rect=self.target)
        self.assertTrue(result.ok)
        weight = result.tensor("weight")

        # Both old shards intersect the target, so both are read.
        self.assertEqual(len(weight.accessed), 2)
        audit = self.access_by_suffix(weight)
        self.assertEqual(
            set(audit),
            {"weight.shard0.cksh", "weight.shard1.cksh"},
        )

        # stride_floats=10 -> 40 bytes per physical row; cols [2,5) are 12
        # bytes starting at offset 8 inside the row.  Header is 38 bytes.
        expected_shard0 = tuple(
            ByteRange(38 + r * 40 + 8, 38 + r * 40 + 20) for r in range(3)
        )
        expected_shard1 = tuple(
            ByteRange(38 + r * 40 + 8, 38 + r * 40 + 20) for r in range(3)
        )
        self.assertEqual(audit["weight.shard0.cksh"], expected_shard0)
        self.assertEqual(audit["weight.shard1.cksh"], expected_shard1)

        for access in weight.accessed:
            assert_ranges_avoid_padding(self, access.path, access.accessed_ranges)

        # The second shard's 6 end-padding bytes must never be touched:
        # last accessed end < file size minus end padding <= file size.
        shard1_path = next(
            a.path for a in weight.accessed if a.path.endswith("shard1.cksh")
        )
        file_size = os.path.getsize(shard1_path)
        last_end = max(r.end for r in audit["weight.shard1.cksh"])
        self.assertLess(last_end, file_size)
        self.assertEqual(file_size - last_end, 40 - 20 + 6)  # row pad (20) + end pad (6)

    def test_publish_writes_readable_new_column_shard(self) -> None:
        result = recover_rank(self.manifest_path, target_rank=7, target_rect=self.target)
        published = publish_recovery(result, self.new_root)
        self.assertTrue(published.ok)
        self.assertEqual(
            os.path.getsize(os.path.join(self.new_root, "manifest.json")),
            os.path.getsize(published.manifest_path),
        )

        reloaded = load_manifest(published.manifest_path)
        self.assertEqual(set(reloaded["tensors"]), {"weight", "optimizer_m"})
        for name, matrix in (("weight", self.weight), ("optimizer_m", self.optimizer)):
            record = reloaded["tensors"][name]["shards"][0]
            self.assertEqual(record["rect"], [0, 0, 6, 3])
            with ShardFile(os.path.join(self.new_root, record["path"])) as shard:
                block = shard.read_rect(Rect(0, 0, 6, 3))
            self.assertEqual(
                block,
                [list(row) for row in slice_matrix(matrix, self.target)],
            )

    def test_failed_recovery_is_never_published(self) -> None:
        # Covered explicitly by the failure test classes below; here we assert
        # a successful recovery leaves no output directory before publishing.
        recover_rank(self.manifest_path, target_rank=7, target_rect=self.target)
        self.assertFalse(os.path.exists(self.new_root))


# ---------------------------------------------------------------------------
# 2. Missing block inside the target -> logical range reported, no publish.
# ---------------------------------------------------------------------------


class MissingBlockTest(RecoveryTestBase):
    def test_missing_block_names_logical_range_and_blocks_publish(self) -> None:
        weight = make_matrix(4, 4, tag=0.0)
        plans = (
            ShardPlan(rect=Rect(0, 0, 2, 4), stride_floats=5),
            ShardPlan(rect=Rect(2, 0, 4, 2)),
        )
        build_checkpoint(
            self.old_root,
            cut_sources(weight, weight, plans),
        )
        target = Rect(0, 0, 4, 4)
        result = recover_rank(self.manifest_path, target_rank=0, target_rect=target)
        self.assertFalse(result.ok)
        self.assertEqual(result.tensors, ())
        self.assertEqual({e.kind for e in result.errors}, {ERROR_MISSING})
        missing = next(e for e in result.errors if e.kind == ERROR_MISSING)
        self.assertIn(Rect(2, 2, 4, 4), missing.ranges)

        with self.assertRaises(Exception):
            publish_recovery(result, self.new_root)
        self.assertFalse(os.path.exists(self.new_root))


# ---------------------------------------------------------------------------
# 3. Overlapping old shards -> overlap logical range reported, no publish.
# ---------------------------------------------------------------------------


class OverlapTest(RecoveryTestBase):
    def test_overlap_names_logical_range_and_blocks_publish(self) -> None:
        weight = make_matrix(4, 4, tag=0.0)
        plans = (
            ShardPlan(rect=Rect(0, 0, 3, 4), stride_floats=6),
            ShardPlan(rect=Rect(2, 0, 4, 4)),
        )
        # build_checkpoint rejects non-tiling plans, so write files directly.
        os.makedirs(self.old_root, exist_ok=True)
        shard0_values = [row[:] for row in weight[0:3]]
        shard1_values = [row[:] for row in weight[2:4]]
        write_shard(
            os.path.join(self.old_root, "weight.shard0.cksh"),
            rows=4,
            cols=4,
            rect=Rect(0, 0, 3, 4),
            stride_floats=6,
            values=shard0_values,
        )
        write_shard(
            os.path.join(self.old_root, "weight.shard1.cksh"),
            rows=4,
            cols=4,
            rect=Rect(2, 0, 4, 4),
            values=shard1_values,
        )
        write_shard(
            os.path.join(self.old_root, "optimizer_m.shard0.cksh"),
            rows=4,
            cols=4,
            rect=Rect(0, 0, 3, 4),
            values=shard0_values,
        )
        write_shard(
            os.path.join(self.old_root, "optimizer_m.shard1.cksh"),
            rows=4,
            cols=4,
            rect=Rect(2, 0, 4, 4),
            values=shard1_values,
        )
        with open(os.path.join(self.old_root, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "format_version": 1,
                    "dtype": "float32-le",
                    "tensors": {
                        name: {
                            "rows": 4,
                            "cols": 4,
                            "shards": [
                                {"path": f"{name}.shard0.cksh", "rect": [0, 0, 3, 4]},
                                {"path": f"{name}.shard1.cksh", "rect": [2, 0, 4, 4]},
                            ],
                        }
                        for name in ("weight", "optimizer_m")
                    },
                },
                fh,
            )

        result = recover_rank(
            self.manifest_path, target_rank=1, target_rect=Rect(0, 0, 4, 4)
        )
        self.assertFalse(result.ok)
        self.assertTrue(all(e.kind == ERROR_OVERLAP for e in result.errors))
        overlap = result.errors[0]
        self.assertIn(Rect(2, 0, 3, 4), overlap.ranges)
        with self.assertRaises(Exception):
            publish_recovery(result, self.new_root)


# ---------------------------------------------------------------------------
# 4. Shape inconsistencies.
# ---------------------------------------------------------------------------


class ShapeMismatchTest(RecoveryTestBase):
    def _build_tiled_checkpoint(self) -> None:
        weight = make_matrix(4, 4, tag=0.0)
        plans = (
            ShardPlan(rect=Rect(0, 0, 2, 4), stride_floats=5),
            ShardPlan(rect=Rect(2, 0, 4, 4)),
        )
        build_checkpoint(self.old_root, cut_sources(weight, weight, plans))

    def test_manifest_rect_disagrees_with_on_disk_header(self) -> None:
        self._build_tiled_checkpoint()
        with open(self.manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        # Lie about the first shard's rectangle: coverage still appears to
        # tile, but the on-disk header tells a different story.
        manifest["tensors"]["weight"]["shards"][0]["rect"] = [0, 0, 2, 3]
        manifest["tensors"]["optimizer_m"]["shards"][0]["rect"] = [0, 0, 2, 3]
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

        result = recover_rank(
            self.manifest_path, target_rank=2, target_rect=Rect(0, 0, 4, 4)
        )
        self.assertFalse(result.ok)
        self.assertTrue(all(e.kind == ERROR_SHAPE_MISMATCH for e in result.errors))
        for error in result.errors:
            self.assertTrue(any(r == Rect(0, 0, 2, 3) for r in error.ranges))
        with self.assertRaises(Exception):
            publish_recovery(result, self.new_root)

    def test_truncated_shard_file_is_reported_with_logical_range(self) -> None:
        self._build_tiled_checkpoint()
        victim = os.path.join(self.old_root, "weight.shard1.cksh")
        # Cut through the second physical row so the header size check fails.
        with open(victim, "r+b") as fh:
            fh.truncate(HEADER_SIZE + 4 * 2)

        result = recover_rank(
            self.manifest_path, target_rank=2, target_rect=Rect(0, 0, 4, 4)
        )
        self.assertFalse(result.ok)
        self.assertTrue(
            all(e.kind in (ERROR_FORMAT, ERROR_SHAPE_MISMATCH) for e in result.errors)
        )
        victim_error = next(e for e in result.errors if e.tensor == "weight")
        self.assertIn(Rect(2, 0, 4, 4), victim_error.ranges)
        with self.assertRaises(Exception):
            publish_recovery(result, self.new_root)


# ---------------------------------------------------------------------------
# 5. Column shards (the other sharding axis) -> narrow row shard.
# ---------------------------------------------------------------------------


class ColumnShardsToRowShardTest(RecoveryTestBase):
    def test_column_shards_assemble_row_target(self) -> None:
        rows, cols = 5, 6
        weight = make_matrix(rows, cols, tag=500.0)
        optimizer = make_matrix(rows, cols, tag=2000.0)
        plans = (
            ShardPlan(rect=Rect(0, 0, 5, 2), stride_floats=3),
            ShardPlan(rect=Rect(0, 2, 5, 4), stride_floats=3),
            ShardPlan(rect=Rect(0, 4, 5, 6)),
        )
        build_checkpoint(self.old_root, cut_sources(weight, optimizer, plans))

        target = Rect(1, 0, 3, 6)  # narrow row shard: 2 rows, full width
        result = recover_rank(self.manifest_path, target_rank=3, target_rect=target)
        self.assertTrue(result.ok, [str(e) for e in result.errors])
        self.assertEqual(result.tensor("weight").values, slice_matrix(weight, target))
        self.assertEqual(
            result.tensor("optimizer_m").values, slice_matrix(optimizer, target)
        )

        # Each of the three column shards contributes; padding is skipped.
        weight = result.tensor("weight")
        self.assertEqual(len(weight.accessed), 3)
        for access in weight.accessed:
            assert_ranges_avoid_padding(self, access.path, access.accessed_ranges)

        published = publish_recovery(result, self.new_root)
        reloaded = load_manifest(published.manifest_path)
        record = reloaded["tensors"]["weight"]["shards"][0]
        self.assertEqual(record["rect"], [0, 0, 2, 6])


# ---------------------------------------------------------------------------
# 6. Geometry and low-level format unit checks.
# ---------------------------------------------------------------------------


class GeometryTest(unittest.TestCase):
    def test_cover_detects_gap_and_overlap(self) -> None:
        target = Rect(0, 0, 4, 4)
        gap_report = cover_rect([Rect(0, 0, 2, 4), Rect(2, 0, 4, 2)], target)
        self.assertFalse(gap_report.ok)
        self.assertEqual(gap_report.uncovered, (Rect(2, 2, 4, 4),))

        overlap_report = cover_rect(
            [Rect(0, 0, 3, 4), Rect(2, 0, 4, 4)], target
        )
        self.assertFalse(overlap_report.ok)
        self.assertEqual(overlap_report.overlaps[0][2], Rect(2, 0, 3, 4))

        ok_report = cover_rect(
            [Rect(0, 0, 4, 2), Rect(0, 2, 4, 4)], target
        )
        self.assertTrue(ok_report.ok)

    def test_pieces_outside_target_are_clipped(self) -> None:
        report = cover_rect(
            [Rect(0, 0, 8, 8)], Rect(2, 2, 5, 5)
        )
        self.assertTrue(report.ok)

    def test_gap_and_overlap_are_both_reported_in_one_band(self) -> None:
        # Columns: [0,2) covered by A, [2,3) is a gap, then B=[3,5) and
        # C=[4,6) overlap on [4,5).  Both defects must be visible.
        target = Rect(0, 0, 1, 6)
        report = cover_rect(
            [Rect(0, 0, 1, 2), Rect(0, 3, 1, 5), Rect(0, 4, 1, 6)],
            target,
        )
        self.assertFalse(report.ok)
        self.assertIn(Rect(0, 2, 1, 3), report.uncovered)
        self.assertEqual(report.overlaps[0][2], Rect(0, 4, 1, 5))

    def test_mixed_row_and_column_pieces_are_swept_in_bands(self) -> None:
        # One wide row piece plus two column pieces covering the lower band.
        target = Rect(0, 0, 4, 4)
        pieces = [
            Rect(0, 0, 2, 4),
            Rect(2, 0, 4, 2),
            Rect(2, 2, 4, 4),
        ]
        self.assertTrue(cover_rect(pieces, target).ok)


class ShardFileRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="shard-roundtrip-")
        self.path = os.path.join(self.tmp, "one.cksh")
        self.rect = Rect(1, 2, 3, 5)
        self.values = [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]
        write_shard(
            self.path,
            rows=4,
            cols=6,
            rect=self.rect,
            stride_floats=5,
            values=self.values,
            end_padding=9,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_partial_read_skips_padding_and_reports_ranges(self) -> None:
        with ShardFile(self.path) as shard:
            header = shard.header
            self.assertEqual(header.stride_floats, 5)
            self.assertEqual(header.end_padding, 9)
            self.assertEqual(os.path.getsize(self.path), 38 + 2 * 20 + 9)

            partial = shard.read_rect(Rect(1, 3, 3, 4))
            self.assertEqual(partial, [[2.0], [5.0]])
            ranges = shard.accessed_ranges
            self.assertEqual(len(ranges), 2)
            # Row 0 local: offset 38 + 0*20 + (3-2)*4 = 42; row 1: 62.
            self.assertEqual(ranges[0], ByteRange(42, 46))
            self.assertEqual(ranges[1], ByteRange(62, 66))

    def test_disjoint_read_touches_nothing(self) -> None:
        with ShardFile(self.path) as shard:
            self.assertEqual(shard.read_rect(Rect(3, 0, 4, 6)), [])
            self.assertEqual(shard.accessed_ranges, ())

    def test_magic_corruption_is_rejected(self) -> None:
        with open(self.path, "r+b") as fh:
            fh.seek(0)
            fh.write(b"XXXX")
        with self.assertRaises(Exception):
            ShardFile(self.path).open()


if __name__ == "__main__":
    unittest.main()
