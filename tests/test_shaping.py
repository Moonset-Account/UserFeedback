"""Tests for shaping: cmap mapping, adjacency-triggered rules, positions."""

import unittest

from fontsubset import MissingMappingError, shape
from fontsubset.shaping import signature

from fixtures import build_ffi_font, build_latin_font


def names(shaped):
    return [g.name for g in shaped]


def positions(shaped):
    return [(g.name, g.x, g.y) for g in shaped]


class TestCmapMapping(unittest.TestCase):
    def setUp(self):
        self.font = build_latin_font()

    def test_plain_text_maps_one_glyph_per_char(self):
        self.assertEqual(names(shape(self.font, "axi")), ["a", "x", "i"])

    def test_missing_mapping_raises(self):
        with self.assertRaises(MissingMappingError) as ctx:
            shape(self.font, "f€")
        self.assertEqual(ctx.exception.codepoint, 0x20AC)

    def test_empty_text_shapes_to_empty_sequence(self):
        self.assertEqual(shape(self.font, ""), [])


class TestAdjacencyRules(unittest.TestCase):
    def setUp(self):
        self.font = build_latin_font()

    def test_ligature_replaces_adjacent_pair(self):
        shaped = shape(self.font, "fi")
        self.assertEqual(names(shaped), ["fi"])
        self.assertEqual(positions(shaped), [("fi", 0, 0)])

    def test_ligature_not_triggered_when_not_adjacent(self):
        self.assertEqual(names(shape(self.font, "fxi")), ["f", "x", "i"])

    def test_contextual_substitution_triggered_by_neighbour(self):
        shaped = shape(self.font, "ab")
        self.assertEqual(positions(shaped), [("a", 0, 0), ("b.alt", 500, 0)])

    def test_contextual_substitution_not_triggered_without_neighbour(self):
        self.assertEqual(names(shape(self.font, "xb")), ["x", "b"])

    def test_positions_accumulate_advances(self):
        shaped = shape(self.font, "axi")
        self.assertEqual(positions(shaped),
                         [("a", 0, 0), ("x", 500, 0), ("i", 980, 0)])

    def test_positions_after_ligature_use_ligature_advance(self):
        shaped = shape(self.font, "fif")  # -> [fi, f]
        self.assertEqual(positions(shaped), [("fi", 0, 0), ("f", 560, 0)])


class TestInterleavedRules(unittest.TestCase):
    """Overlapping/adjacent rule matches; outcome depends on rule order."""

    def test_ff_ligature_first_enables_ffi(self):
        font = build_ffi_font(rule_order=0)
        self.assertEqual(names(shape(font, "ffi")), ["ffi"])

    def test_fi_ligature_first_blocks_ffi(self):
        font = build_ffi_font(rule_order=1)
        self.assertEqual(names(shape(font, "ffi")), ["f", "fi"])

    def test_left_to_right_non_overlapping_scan(self):
        font = build_ffi_font(rule_order=0)
        self.assertEqual(names(shape(font, "fff")), ["ff", "f"])

    def test_repeated_ligatures(self):
        font = build_ffi_font(rule_order=0)
        shaped = shape(font, "fifi")
        self.assertEqual(names(shaped), ["fi", "fi"])
        self.assertEqual(positions(shaped), [("fi", 0, 0), ("fi", 540, 0)])

    def test_signature_is_order_and_position_sensitive(self):
        font = build_ffi_font(rule_order=0)
        self.assertNotEqual(signature(shape(font, "fi")),
                            signature(shape(font, "if")))


if __name__ == "__main__":
    unittest.main()
