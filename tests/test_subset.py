"""Tests for subsetting: closure, privacy of components, renumbering,
cycle reporting, dedup and playback verification."""

import unittest

from fontsubset import (
    CompositeCycleError,
    MissingMappingError,
    shape,
    subset_font,
)
from fontsubset.shaping import signature

from fixtures import (
    CYRILLIC_A,
    FULLWIDTH_A,
    LATIN_PUBLIC_TEXTS,
    build_cycle_font,
    build_ffi_font,
    build_latin_font,
    build_self_cycle_font,
    build_unreachable_cycle_font,
)


class TestClosureAndRenumbering(unittest.TestCase):
    def setUp(self):
        self.font = build_latin_font()
        self.result = subset_font(self.font, LATIN_PUBLIC_TEXTS)
        self.subset = self.result.font

    def test_renumbering_is_compact_and_notdef_first(self):
        n = len(self.subset.glyphs)
        # dedup may merge several old gids into one new gid, but the
        # resulting numbering must be exactly 0..n-1 with .notdef at 0
        self.assertEqual(set(self.result.old_to_new.values()),
                         set(range(n)))
        self.assertEqual(self.subset.glyphs[0].name, ".notdef")
        self.assertEqual(len(self.result.kept_glyph_names), n)

    def test_needed_glyphs_kept(self):
        kept = set(self.result.kept_glyph_names)
        # direct shaping output + transitive composite components
        # ("i.copy" is reachable but merged into "i" by outline dedup)
        for name in ("f", "i", "fi", "a", "b", "b.alt",
                     "x", "A", "space"):
            self.assertIn(name, kept)

    def test_unneeded_glyphs_dropped(self):
        self.assertIn("unused", self.result.dropped_glyph_names)
        self.assertIn("dotlessi", self.result.dropped_glyph_names)
        self.assertNotIn("unused", self.result.kept_glyph_names)

    def test_old_to_new_consistent_with_subset_cmap(self):
        for cp, new_gid in self.subset.cmap.items():
            old_gid = self.font.cmap[cp]
            self.assertEqual(self.result.old_to_new[old_gid], new_gid)

    def test_rules_filtered_and_remapped(self):
        names = self.subset.name_to_gid
        rule_pairs = {
            (tuple(self.subset.glyph_name(g) for g in r.input),
             tuple(self.subset.glyph_name(g) for g in r.output))
            for r in self.subset.rules
        }
        self.assertIn((("f", "i"), ("fi",)), rule_pairs)
        self.assertIn((("a", "b"), ("a", "b.alt")), rule_pairs)
        # rule referencing the dropped "unused" glyph must be gone
        for inp, out in rule_pairs:
            self.assertNotIn("unused", inp)
            self.assertNotIn("unused", out)


class TestMappingPrivacy(unittest.TestCase):
    """Removed characters must not be recoverable; internal components
    must not gain externally visible mappings."""

    def setUp(self):
        self.font = build_latin_font()
        self.result = subset_font(self.font, LATIN_PUBLIC_TEXTS)
        self.subset = self.result.font

    def test_subset_cmap_contains_exactly_public_codepoints(self):
        expected = {ord(c) for t in LATIN_PUBLIC_TEXTS for c in t}
        self.assertEqual(set(self.subset.cmap), expected)
        self.assertEqual(self.result.public_codepoints, frozenset(expected))

    def test_removed_char_sharing_same_glyph_not_recoverable(self):
        # FULLWIDTH_A mapped to the very same glyph "A" as public "A";
        # the glyph survives, the code point must not.
        self.assertIn("A", self.result.kept_glyph_names)
        self.assertNotIn(FULLWIDTH_A, self.subset.cmap)

    def test_removed_char_sharing_outline_not_recoverable(self):
        # CYRILLIC_A had a distinct glyph with an outline identical to
        # public "A"; no trace of the code point may remain.
        self.assertNotIn(CYRILLIC_A, self.subset.cmap)
        self.assertNotIn("A.cyr", self.result.kept_glyph_names)

    def test_internal_components_have_no_cmap_entry(self):
        cmap_gids = set(self.subset.cmap.values())
        for internal in ("fi", "i.copy"):
            gid = self.subset.name_to_gid.get(internal)
            if gid is not None:  # i.copy may be merged into i by dedup
                self.assertNotIn(gid, cmap_gids,
                                 f"component {internal} gained a mapping")

    def test_dedup_merges_component_copy_into_public_glyph(self):
        # i.copy (component-only) duplicates i's outline; dedup must
        # merge it into the public glyph, not the other way round.
        self.assertNotIn("i.copy", self.result.kept_glyph_names)
        fi = self.subset.glyphs[self.subset.name_to_gid["fi"]]
        comp_names = [self.subset.glyph_name(c.glyph)
                      for c in fi.components]
        self.assertEqual(comp_names, ["f", "i"])

    def test_dedup_disabled_keeps_duplicate_but_still_unmapped(self):
        result = subset_font(self.font, LATIN_PUBLIC_TEXTS,
                             deduplicate=False)
        subset = result.font
        self.assertIn("i.copy", result.kept_glyph_names)
        gid = subset.name_to_gid["i.copy"]
        self.assertNotIn(gid, set(subset.cmap.values()))
        self.assertNotIn(CYRILLIC_A, subset.cmap)
        self.assertNotIn(FULLWIDTH_A, subset.cmap)


class TestPlaybackVerification(unittest.TestCase):
    def test_all_public_texts_reproduce_exactly(self):
        font = build_latin_font()
        result = subset_font(font, LATIN_PUBLIC_TEXTS)
        self.assertTrue(result.report.ok)
        self.assertEqual(len(result.report.records),
                         len(LATIN_PUBLIC_TEXTS))
        for text in LATIN_PUBLIC_TEXTS:
            self.assertEqual(signature(shape(font, text)),
                             signature(shape(result.font, text)))

    def test_interleaved_ligature_font_roundtrip(self):
        texts = ["ffi", "fifi", "fff", "fi", "i"]
        for order in (0, 1):
            font = build_ffi_font(rule_order=order)
            result = subset_font(font, texts)
            self.assertTrue(result.report.ok)
            for text in texts:
                self.assertEqual(signature(shape(font, text)),
                                 signature(shape(result.font, text)))

    def test_empty_public_list_yields_notdef_only(self):
        result = subset_font(build_latin_font(), [])
        self.assertEqual(result.kept_glyph_names, (".notdef",))
        self.assertEqual(result.font.cmap, {})
        self.assertEqual(result.font.rules, [])
        self.assertTrue(result.report.ok)


class TestCycleDetection(unittest.TestCase):
    def test_three_glyph_cycle_reports_involved_components(self):
        with self.assertRaises(CompositeCycleError) as ctx:
            subset_font(build_cycle_font(), ["z"])
        cycle = ctx.exception.cycle
        self.assertEqual(cycle[0], cycle[-1])
        self.assertEqual(set(cycle[:-1]), {"c1", "c2", "c3"})
        self.assertIn("c1", str(ctx.exception))
        self.assertIn("c3", str(ctx.exception))

    def test_self_cycle(self):
        with self.assertRaises(CompositeCycleError) as ctx:
            subset_font(build_self_cycle_font(), ["s"])
        self.assertEqual(ctx.exception.cycle, ["s", "s"])

    def test_unreachable_cycle_does_not_block_subsetting(self):
        result = subset_font(build_unreachable_cycle_font(), ["a"])
        self.assertEqual(set(result.kept_glyph_names), {".notdef", "a"})
        self.assertIn("c1", result.dropped_glyph_names)
        self.assertTrue(result.report.ok)


class TestPublicInputValidation(unittest.TestCase):
    def test_public_text_with_unmapped_character_raises(self):
        with self.assertRaises(MissingMappingError) as ctx:
            subset_font(build_latin_font(), ["fi€"])
        self.assertEqual(ctx.exception.codepoint, 0x20AC)


if __name__ == "__main__":
    unittest.main()
