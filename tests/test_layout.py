"""Unit tests for gradient_layout (run: python3 -m unittest discover -s tests -v)."""

import math
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradient_layout import (  # noqa: E402
    GradientLayout,
    LayoutError,
    ParamSpec,
)


def flat_bytes(buffers):
    return b"".join(bytes(b) for b in buffers)


class TestDeclarationAndLayout(unittest.TestCase):
    def test_declaration_order_alignment_and_padding(self):
        layout = GradientLayout(
            [
                ParamSpec("a", (3,), width=4),          # 12 bytes
                ParamSpec("frozen_bn", (4,), width=4, frozen=True),
                ParamSpec("b", (2, 2), width=8),        # 32 bytes
                ParamSpec("c", (1,), width=2, kind="int"),
            ],
            alignment=16,
            num_ranks=2,
        )
        self.assertEqual(list(layout.params), ["a", "b", "c"])  # frozen excluded
        self.assertEqual(layout.params["a"].offset, 0)
        self.assertEqual(layout.params["b"].offset, 16)   # 12 aligned up to 16
        self.assertEqual(layout.params["c"].offset, 48)   # 16+32
        for p in layout.params.values():
            self.assertEqual(p.offset % 16, 0)
        # total is a multiple of lcm(alignment, num_ranks) and splits evenly
        self.assertEqual(layout.total_bytes % math.lcm(16, 2), 0)
        self.assertEqual(layout.total_bytes, 64)          # 50 -> 64
        self.assertEqual(layout.rank_bytes, 32)
        self.assertEqual(
            layout.padding_spans(), [(12, 16), (50, 64)]
        )

    def test_frozen_parameters_keep_no_layout(self):
        layout = GradientLayout(
            [ParamSpec("x", (2,), frozen=True), ParamSpec("y", (2,))],
            alignment=4,
            num_ranks=1,
        )
        self.assertNotIn("x", layout.params)
        self.assertEqual(layout.params["y"].offset, 0)  # no hole left behind

    def test_invalid_declarations_rejected(self):
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("a", (2,)), ParamSpec("a", (3,))])
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("", (2,))])
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("a", ())])
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("a", (0, 2))])
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("a", (2,), width=3)])  # no 3-byte dtype
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("a", (2,))], alignment=0)
        with self.assertRaises(LayoutError):
            GradientLayout([ParamSpec("a", (2,))], num_ranks=0)


class TestGatherScatter(unittest.TestCase):
    def setUp(self):
        # alignment=8: a at 0..12, b at 16..28, m at 32..56; padding spans
        # (12,16) and (28,32); total 56, num_ranks=2 -> rank_bytes=28.
        self.layout = GradientLayout(
            [
                ParamSpec("a", (3,), width=4),
                ParamSpec("b", (3,), width=4),
                ParamSpec("m", (2, 3), width=4),
            ],
            alignment=8,
            num_ranks=2,
        )
        self.assertEqual(self.layout.rank_bytes, 28)
        self.buffers = self.layout.new_buffers()

    def test_scatter_then_gather_roundtrip(self):
        self.layout.scatter(self.buffers, "a", [1.0, 2.0, 3.0])
        self.assertEqual(self.layout.gather(self.buffers, "a"), [1.0, 2.0, 3.0])

    def test_param_spanning_rank_boundary(self):
        # Three 12-byte params, alignment=4, num_ranks=2 -> rank_bytes=18.
        # b occupies absolute bytes 12..24, crossing the rank boundary at 18,
        # and element b[1] (bytes 16..20) itself straddles the boundary.
        layout = GradientLayout(
            [
                ParamSpec("a", (3,), width=4),
                ParamSpec("b", (3,), width=4),
                ParamSpec("c", (3,), width=4),
            ],
            alignment=4,
            num_ranks=2,
        )
        self.assertEqual(layout.rank_bytes, 18)
        buffers = layout.new_buffers()
        layout.scatter(buffers, "b", [10.0, 20.0, 30.0])
        self.assertEqual(layout.gather(buffers, "b"), [10.0, 20.0, 30.0])
        raw = flat_bytes(buffers)
        self.assertEqual(struct.unpack("<f", raw[16:20])[0], 20.0)
        self.assertNotEqual(raw[18:20], b"\x00\x00")  # really split across ranks
        # neighbours on either side of the boundary are unaffected
        layout.scatter(buffers, "a", [1.0, 2.0, 3.0])
        layout.scatter(buffers, "c", [4.0, 5.0, 6.0])
        self.assertEqual(layout.gather(buffers, "b"), [10.0, 20.0, 30.0])

    def test_rectangular_slice_gather_and_scatter(self):
        self.layout.scatter(
            self.buffers, "m", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        )
        # column slice: logical rows are strided in the flat buffer
        self.assertEqual(
            self.layout.gather(self.buffers, "m", (slice(None), slice(1, 3))),
            [[2.0, 3.0], [5.0, 6.0]],
        )
        self.layout.scatter(
            self.buffers, "m", [[10.0], [40.0]], (slice(None), 0)
        )
        self.assertEqual(
            self.layout.gather(self.buffers, "m"),
            [[10.0, 2.0, 3.0], [40.0, 5.0, 6.0]],
        )

    def test_integer_and_negative_indices(self):
        self.layout.scatter(self.buffers, "a", [1.0, 2.0, 3.0])
        self.assertEqual(self.layout.gather(self.buffers, "a", 1), 2.0)
        self.assertEqual(self.layout.gather(self.buffers, "a", -1), 3.0)
        self.layout.scatter(self.buffers, "a", 9.0, -2)
        self.assertEqual(self.layout.gather(self.buffers, "a"), [1.0, 9.0, 3.0])

    def test_partial_selection_defaults_to_full_rows(self):
        self.layout.scatter(
            self.buffers, "m", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        )
        self.assertEqual(self.layout.gather(self.buffers, "m", 0), [1.0, 2.0, 3.0])
        self.assertEqual(
            self.layout.gather(self.buffers, "m", (slice(None),)),
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
        )

    def test_padding_never_touched(self):
        self.layout.scatter(self.buffers, "a", [1.0, 2.0, 3.0])
        self.layout.scatter(self.buffers, "b", [4.0, 5.0, 6.0])
        self.layout.scatter(self.buffers, "m", 7.0, (0, 0))
        raw = flat_bytes(self.buffers)
        for start, stop in self.layout.padding_spans():
            self.assertEqual(raw[start:stop], b"\x00" * (stop - start))

    def test_neighbouring_params_untouched_by_scatter(self):
        self.layout.scatter(self.buffers, "a", [1.0, 1.0, 1.0])
        self.layout.scatter(self.buffers, "b", [2.0, 2.0, 2.0])
        before = flat_bytes(self.buffers)
        # overwrite every element of b via a full slice
        self.layout.scatter(self.buffers, "b", [3.0, 3.0, 3.0], (slice(0, 3),))
        after = flat_bytes(self.buffers)
        a_lo, a_hi = self.layout.params["a"].byte_span
        m_lo, m_hi = self.layout.params["m"].byte_span
        self.assertEqual(before[a_lo:a_hi], after[a_lo:a_hi])
        self.assertEqual(before[m_lo:m_hi], after[m_lo:m_hi])

    def test_out_of_bounds_rejected(self):
        cases = [
            (3,),            # int index past the end
            (-4,),           # negative index past the start
            (slice(0, 4),),  # slice stop past the end
            (slice(-4, 2),), # negative start past the start
            (slice(2, 1),),  # start > stop
            (slice(0, 3, 2),),  # non-unit step is not a rectangle
            (0, 0),          # too many indices for a 1-D param
        ]
        for sel in cases:
            with self.subTest(selection=sel):
                with self.assertRaises((IndexError, TypeError)):
                    self.layout.gather(self.buffers, "a", sel)
                with self.assertRaises((IndexError, TypeError)):
                    self.layout.scatter(self.buffers, "a", [0.0], sel)

    def test_unknown_and_frozen_names_rejected(self):
        with self.assertRaises(KeyError):
            self.layout.gather(self.buffers, "nope")
        frozen = GradientLayout(
            [ParamSpec("fr", (2,), frozen=True)], alignment=4, num_ranks=1
        )
        with self.assertRaises(KeyError):
            frozen.gather(frozen.new_buffers(), "fr")

    def test_buffer_shape_validation(self):
        with self.assertRaises(LayoutError):
            self.layout.gather([bytearray(28)], "a")            # too few ranks
        with self.assertRaises(LayoutError):
            self.layout.gather(self.buffers + [bytearray(28)], "a")
        with self.assertRaises(LayoutError):
            self.layout.gather([bytearray(28), bytearray(5)], "a")
        with self.assertRaises(LayoutError):
            self.layout.scatter([bytes(28), bytes(28)], "a", [1.0, 2.0, 3.0])

    def test_value_count_mismatch_rejected(self):
        with self.assertRaises(LayoutError):
            self.layout.scatter(self.buffers, "a", [1.0, 2.0])
        with self.assertRaises(LayoutError):
            self.layout.scatter(self.buffers, "m", [1.0, 2.0, 3.0], (slice(None), 0))

    def test_nested_and_flat_value_forms_equivalent(self):
        other = self.layout.new_buffers()
        self.layout.scatter(self.buffers, "m", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.layout.scatter(other, "m", [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(flat_bytes(self.buffers), flat_bytes(other))

    def test_dtypes(self):
        layout = GradientLayout(
            [
                ParamSpec("i", (2,), width=1, kind="int"),
                ParamSpec("u", (2,), width=2, kind="uint"),
                ParamSpec("d", (2,), width=8, kind="float"),
            ],
            alignment=1,
            num_ranks=1,
        )
        buf = layout.new_buffers()
        layout.scatter(buf, "i", [-1, 127])
        layout.scatter(buf, "u", [0, 65535])
        layout.scatter(buf, "d", [0.5, -2.25])
        self.assertEqual(layout.gather(buf, "i"), [-1, 127])
        self.assertEqual(layout.gather(buf, "u"), [0, 65535])
        self.assertEqual(layout.gather(buf, "d"), [0.5, -2.25])


class TestRankCountInvariance(unittest.TestCase):
    """Same logical updates, different rank counts -> identical parameters."""

    SPECS = [
        ParamSpec("w1", (2, 3), width=4),
        ParamSpec("w2", (4,), width=4),
        ParamSpec("frozen", (5,), width=4, frozen=True),
        ParamSpec("w3", (2, 2, 2), width=4, kind="int"),
    ]
    UPDATES = [
        ("w1", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
        ("w1", (slice(1, 2), slice(0, 2)), [[9.5, 8.5]]),
        ("w2", 2, 7.0),
        ("w2", (slice(0, 2),), [0.5, 0.25]),
        ("w3", (1,), [[1, 2], [3, 4]]),
        ("w3", (0, 1, 0), -9),
    ]

    def rebuild_with(self, num_ranks):
        layout = GradientLayout(self.SPECS, alignment=8, num_ranks=num_ranks)
        buffers = layout.apply_updates(self.UPDATES)
        return layout, layout.rebuild(buffers)

    def test_rebuild_identical_across_rank_counts(self):
        _, reference = self.rebuild_with(1)
        for ranks in (2, 3, 5, 8):
            with self.subTest(num_ranks=ranks):
                _, rebuilt = self.rebuild_with(ranks)
                self.assertEqual(rebuilt, reference)

    def test_expected_values_and_clean_padding(self):
        layout, rebuilt = self.rebuild_with(3)
        self.assertEqual(rebuilt["w1"], [[1.0, 2.0, 3.0], [9.5, 8.5, 6.0]])
        self.assertEqual(rebuilt["w2"], [0.5, 0.25, 7.0, 0.0])
        self.assertEqual(
            rebuilt["w3"], [[[0, 0], [-9, 0]], [[1, 2], [3, 4]]]
        )
        self.assertNotIn("frozen", rebuilt)
        buffers = layout.apply_updates(self.UPDATES)
        raw = flat_bytes(buffers)
        for start, stop in layout.padding_spans():
            self.assertEqual(raw[start:stop], b"\x00" * (stop - start))

    def test_interleaved_updates_across_ranks(self):
        # Interleave updates to different params; each rank count must agree.
        layouts = {
            n: GradientLayout(self.SPECS, alignment=8, num_ranks=n)
            for n in (1, 2, 4)
        }
        buffers = {n: layouts[n].new_buffers() for n in layouts}
        sequence = [
            ("w2", (slice(1, 3),), [3.0, 4.0]),
            ("w1", (0, slice(1, 3)), [5.0, 6.0]),
            ("w3", (slice(None), 0, 1), [7, 8]),
            ("w2", 0, 1.0),
            ("w1", (1, 2), 2.0),
        ]
        for name, sel, values in sequence:
            for n in layouts:
                layouts[n].scatter(buffers[n], name, values, sel)
        rebuilt = {n: layouts[n].rebuild(buffers[n]) for n in layouts}
        self.assertEqual(rebuilt[2], rebuilt[1])
        self.assertEqual(rebuilt[4], rebuilt[1])
        self.assertEqual(rebuilt[1]["w2"], [1.0, 3.0, 4.0, 0.0])
        self.assertEqual(rebuilt[1]["w1"], [[0.0, 5.0, 6.0], [0.0, 0.0, 2.0]])
        self.assertEqual(
            rebuilt[1]["w3"],
            [[[0, 7], [0, 0]], [[0, 8], [0, 0]]],
        )


if __name__ == "__main__":
    unittest.main()
