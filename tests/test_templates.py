"""Template parsing, matching, and exact set algebra."""

from __future__ import annotations

import itertools
import unittest

from gateway_routing import PathError, TemplateError, analyze_templates, parse_template, split_path


def matched_set(template: str, alphabet=("a", "b", "c"), max_len: int = 4) -> set[str]:
    """All canonical paths over ``alphabet`` up to ``max_len`` matched by ``template``."""

    parsed = parse_template(template)
    found: set[str] = set()
    for length in range(max_len + 1):
        for combo in itertools.product(alphabet, repeat=length):
            path = "/" + "/".join(combo) if combo else "/"
            if parsed.match_path(path) is not None:
                found.add(path)
    return found


class SplitPathTests(unittest.TestCase):
    def test_root(self):
        self.assertEqual(split_path("/"), ())

    def test_simple(self):
        self.assertEqual(split_path("/a/b"), ("a", "b"))

    def test_rejects_missing_leading_slash(self):
        with self.assertRaises(PathError):
            split_path("a/b")

    def test_rejects_trailing_slash(self):
        with self.assertRaises(PathError):
            split_path("/a/")

    def test_rejects_double_slash(self):
        with self.assertRaises(PathError):
            split_path("/a//b")

    def test_rejects_non_string(self):
        with self.assertRaises(PathError):
            split_path(3)  # type: ignore[arg-type]

    def test_empty_string(self):
        with self.assertRaises(PathError):
            split_path("")


class ParseTemplateTests(unittest.TestCase):
    def test_literal_and_param(self):
        template = parse_template("/a/{x}")
        self.assertEqual(template.prefix_length, 2)
        self.assertFalse(template.has_star)

    def test_root(self):
        template = parse_template("/")
        self.assertEqual(template.prefix, ())
        self.assertFalse(template.has_star)
        self.assertIsNotNone(template.match_path("/"))

    def test_star(self):
        template = parse_template("/a/{x}/**")
        self.assertTrue(template.has_star)
        self.assertEqual(template.prefix_length, 2)

    def test_star_only(self):
        template = parse_template("/**")
        self.assertTrue(template.has_star)
        self.assertEqual(template.prefix_length, 0)

    def test_rejects_star_in_middle(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/**/b")

    def test_rejects_partial_brace(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/x{y}")

    def test_rejects_empty_param(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/{}")

    def test_rejects_double_brace(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/{{x}}")

    def test_rejects_nested_brace(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/{x{y}}")

    def test_rejects_whitespace_in_param(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/{x y}")

    def test_rejects_star_in_param_name(self):
        # '{**}' or '{x*}' would be visually confusable with the wildcard.
        with self.assertRaises(TemplateError):
            parse_template("/a/{**}")
        with self.assertRaises(TemplateError):
            parse_template("/a/{x*}")

    def test_single_star_segment_is_a_literal(self):
        template = parse_template("/a/*")
        self.assertFalse(template.has_star)
        self.assertEqual(template.match_path("/a/*"), {})
        self.assertIsNone(template.match_path("/a/anything"))

    def test_star_like_literal_text_is_supported(self):
        template = parse_template("/a/b**c")
        self.assertFalse(template.has_star)
        self.assertEqual(template.match_path("/a/b**c"), {})

    def test_rejects_empty_segment(self):
        with self.assertRaises(TemplateError):
            parse_template("/a//b")

    def test_rejects_trailing_slash(self):
        with self.assertRaises(TemplateError):
            parse_template("/a/")

    def test_rejects_missing_leading_slash(self):
        with self.assertRaises(TemplateError):
            parse_template("a/b")

    def test_rejects_non_string(self):
        with self.assertRaises(TemplateError):
            parse_template(None)  # type: ignore[arg-type]

    def test_rejects_empty_string(self):
        with self.assertRaises(TemplateError):
            parse_template("")

    def test_star_segment_is_the_final_star(self):
        # A trailing '**' is the wildcard, never a literal segment.
        template = parse_template("/a/**")
        self.assertTrue(template.has_star)
        self.assertEqual(template.prefix_length, 1)

    def test_brace_literal_impossible(self):
        # Braces always introduce a parameter, so they cannot be literal text.
        with self.assertRaises(TemplateError):
            parse_template("/{a}b")


class MatchTests(unittest.TestCase):
    def test_literal_match(self):
        template = parse_template("/a/b")
        self.assertEqual(template.match_path("/a/b"), {})
        self.assertIsNone(template.match_path("/a/c"))
        self.assertIsNone(template.match_path("/a"))
        self.assertIsNone(template.match_path("/a/b/c"))

    def test_param_capture(self):
        template = parse_template("/a/{x}")
        self.assertEqual(template.match_path("/a/hello"), {"x": "hello"})
        self.assertIsNone(template.match_path("/a"))

    def test_star_matches_zero_segments(self):
        template = parse_template("/a/{x}/**")
        self.assertEqual(template.match_path("/a/1"), {"x": "1"})
        self.assertEqual(template.match_path("/a/1/b/c"), {"x": "1"})

    def test_star_only_matches_root(self):
        template = parse_template("/**")
        self.assertEqual(template.match_path("/"), {})

    def test_duplicate_param_last_wins(self):
        template = parse_template("/a/{x}/{x}")
        self.assertEqual(template.match_path("/a/1/2"), {"x": "2"})

    def test_param_matches_any_nonempty_segment(self):
        template = parse_template("/{x}")
        for value in ("a", "a-b", "0", "..", "%20", "ü"):
            self.assertEqual(template.match_path("/" + value), {"x": value})


class AnalyzeTests(unittest.TestCase):
    def test_canonical_example_overlap(self):
        pair = analyze_templates("/a/{x}", "/{y}/b")
        self.assertTrue(pair.intersects)
        self.assertFalse(pair.a_subset_b)
        self.assertFalse(pair.b_subset_a)
        self.assertEqual(pair.kind, "overlap")
        self.assertTrue(pair.conflict)
        self.assertIsNotNone(pair.witness_path)
        self.assertEqual(parse_template("/a/{x}").match_path(pair.witness_path), {"x": "b"})
        self.assertEqual(parse_template("/{y}/b").match_path(pair.witness_path), {"y": "a"})

    def test_overlap_witness_is_a_real_path(self):
        pair = analyze_templates("/a/{x}", "/{y}/b")
        self.assertEqual(pair.witness_path, "/a/b")

    def test_literal_subset_param(self):
        pair = analyze_templates("/a/{x}", "/a/b")
        self.assertTrue(pair.intersects)
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.a_subset_b)
        self.assertFalse(pair.conflict)
        self.assertEqual(pair.kind, "b_subset_a")

    def test_equal_languages_conflict(self):
        pair = analyze_templates("/a/{x}", "/a/{y}")
        self.assertTrue(pair.equal)
        self.assertTrue(pair.conflict)
        self.assertEqual(pair.kind, "equal")

    def test_longer_prefix_star_is_contained_by_shorter_star(self):
        pair = analyze_templates("/a/**", "/a/{x}/**")
        self.assertTrue(pair.intersects)
        self.assertFalse(pair.equal)
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.conflict)

    def test_literal_star_is_strictly_contained_by_param_star(self):
        pair = analyze_templates("/a/**", "/{x}/**")
        self.assertTrue(pair.a_subset_b)
        self.assertFalse(pair.b_subset_a)
        self.assertFalse(pair.conflict)
        # The containment witness lives outside /a.
        self.assertIsNone(parse_template("/a/**").match_path("/b"))

    def test_star_contains_finite(self):
        pair = analyze_templates("/a/**", "/a/b/c")
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.a_subset_b)
        self.assertFalse(pair.conflict)

    def test_star_contains_prefix_itself(self):
        pair = analyze_templates("/a/**", "/a")
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.conflict)

    def test_star_exposes_extra_path(self):
        pair = analyze_templates("/a/**", "/a/{x}")
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.a_subset_b)

    def test_disjoint_literals(self):
        pair = analyze_templates("/a/{x}", "/b/{x}")
        self.assertFalse(pair.intersects)
        self.assertIsNone(pair.witness_path)
        self.assertFalse(pair.conflict)
        self.assertEqual(pair.kind, "disjoint")

    def test_disjoint_stars(self):
        pair = analyze_templates("/a/**", "/b/**")
        self.assertFalse(pair.intersects)

    def test_star_wildcard_universal(self):
        pair = analyze_templates("/**", "/a/b")
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.conflict)

    def test_star_and_root(self):
        pair = analyze_templates("/**", "/")
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.a_subset_b)

    def test_short_star_vs_longer_star_overlap(self):
        pair = analyze_templates("/a/**", "/{x}/b/**")
        self.assertTrue(pair.intersects)
        self.assertFalse(pair.a_subset_b)
        self.assertFalse(pair.b_subset_a)
        self.assertTrue(pair.conflict)

    def test_param_star_vs_finite_longer(self):
        pair = analyze_templates("/{x}/**", "/{y}/b")
        self.assertTrue(pair.b_subset_a)
        self.assertFalse(pair.conflict)

    def test_intersection_requires_matching_literals(self):
        # Lengths are compatible, but the second segment literal disagrees.
        pair = analyze_templates("/a/{x}/c", "/a/{y}/d")
        self.assertFalse(pair.intersects)

    def test_star_intersection_with_mismatched_prefix(self):
        pair = analyze_templates("/a/b/**", "/a/c")
        self.assertFalse(pair.intersects)

    def test_equal_root_only(self):
        pair = analyze_templates("/", "/")
        self.assertTrue(pair.equal)
        self.assertTrue(pair.conflict)

    def test_accepts_parsed_templates(self):
        pair = analyze_templates(parse_template("/a/{x}"), parse_template("/a/b"))
        self.assertTrue(pair.b_subset_a)


class WitnessSoundnessTests(unittest.TestCase):
    """Every reported witness must genuinely be matched by both templates."""

    TEMPLATES = [
        "/",
        "/a",
        "/a/b",
        "/a/{x}",
        "/{x}/b",
        "/{x}/{y}",
        "/a/{x}/c",
        "/a/**",
        "/**",
        "/{x}/**",
        "/a/{x}/**",
        "/{x}/b/**",
        "/a/b/**",
        "/a/b/c",
        "/a/b/{x}",
        "/a/{x}/b/{y}",
        "/x/{p}/**",
    ]

    def test_all_pairs(self):
        for left, right in itertools.combinations(self.TEMPLATES, 2):
            pair = analyze_templates(left, right)
            if not pair.intersects:
                self.assertIsNone(pair.witness_path)
                continue
            self.assertIsNotNone(pair.witness_path)
            witness = pair.witness_path
            self.assertIsNotNone(
                parse_template(left).match_path(witness),
                f"{witness} should match {left}",
            )
            self.assertIsNotNone(
                parse_template(right).match_path(witness),
                f"{witness} should match {right}",
            )

    def test_all_pairs_are_antisymmetric(self):
        for left, right in itertools.combinations(self.TEMPLATES, 2):
            forward = analyze_templates(left, right)
            backward = analyze_templates(right, left)
            self.assertEqual(forward.a_subset_b, backward.b_subset_a)
            self.assertEqual(forward.b_subset_a, backward.a_subset_b)
            self.assertEqual(forward.equal, backward.equal)
            self.assertEqual(forward.intersects, backward.intersects)


class SubsetSoundnessTests(unittest.TestCase):
    """A subset verdict must hold on every sampled path (necessary condition)."""

    TEMPLATES = WitnessSoundnessTests.TEMPLATES
    ALPHABET = ("a", "b", "c")

    def test_subsets_hold_on_sampled_paths(self):
        universes = {t: matched_set(t, self.ALPHABET, 4) for t in self.TEMPLATES}
        for left, right in itertools.combinations(self.TEMPLATES, 2):
            pair = analyze_templates(left, right)
            if pair.a_subset_b:
                self.assertLessEqual(
                    universes[left], universes[right], f"{left} should be a subset of {right}"
                )
            if pair.b_subset_a:
                self.assertLessEqual(
                    universes[right], universes[left], f"{right} should be a subset of {left}"
                )
            if pair.equal:
                self.assertEqual(universes[left], universes[right])

    def test_disjoint_verdict_has_no_sampled_intersection(self):
        universes = {t: matched_set(t, self.ALPHABET, 4) for t in self.TEMPLATES}
        for left, right in itertools.combinations(self.TEMPLATES, 2):
            pair = analyze_templates(left, right)
            if not pair.intersects:
                self.assertFalse(universes[left] & universes[right])


class StarFreeExactnessTests(unittest.TestCase):
    """For star-free templates, bounded enumeration over literals + a fresh
    segment is a complete decision procedure, so verdicts must agree exactly.
    """

    TEMPLATES = [
        "/",
        "/a",
        "/b",
        "/a/b",
        "/a/a",
        "/{x}",
        "/{x}/{y}",
        "/a/{x}",
        "/{x}/a",
        "/a/{x}/b",
        "/{x}/a/{y}",
        "/a/a/b",
        "/a/{x}/{y}",
    ]
    ALPHABET = ("a", "b", "zz")

    def test_exact_verdicts(self):
        universes = {t: matched_set(t, self.ALPHABET, 3) for t in self.TEMPLATES}
        for left, right in itertools.combinations(self.TEMPLATES, 2):
            pair = analyze_templates(left, right)
            self.assertEqual(
                pair.intersects,
                bool(universes[left] & universes[right]),
                f"intersection verdict wrong for {left} vs {right}",
            )
            self.assertEqual(
                pair.a_subset_b,
                universes[left] <= universes[right],
                f"a_subset_b wrong for {left} vs {right}",
            )
            self.assertEqual(
                pair.b_subset_a,
                universes[right] <= universes[left],
                f"b_subset_a wrong for {left} vs {right}",
            )
            self.assertEqual(pair.equal, universes[left] == universes[right])


class ExhaustiveGrammarEquivalenceTests(unittest.TestCase):
    """Brute-force oracle over the whole small grammar.

    Every template in this family has at most three fixed segments, so two
    languages differ (or a subset claim fails) only on a path of at most
    four segments.  Enumerating all paths over ``{"a", "b", "zz"}`` up to
    length four is therefore an exact decision procedure, independent of the
    symbolic analysis under test.
    """

    SEGMENTS = ("a", "b", "{x}", "{y}")
    ALPHABET = ("a", "b", "zz")
    MAX_PROBE_LEN = 4

    @classmethod
    def setUpClass(cls) -> None:
        cls.universe = []
        for length in range(cls.MAX_PROBE_LEN + 1):
            for combo in itertools.product(cls.ALPHABET, repeat=length):
                cls.universe.append("/" + "/".join(combo) if combo else "/")
        cls.matched = {}
        for prefix_len in range(3):
            for combo in itertools.product(cls.SEGMENTS, repeat=prefix_len):
                for has_star in (False, True):
                    text = "/" + "/".join(combo)
                    if not combo:
                        text = "/"
                    if has_star:
                        text = text + ("" if text.endswith("/") else "/") + "**"
                    template = parse_template(text)
                    cls.matched[text] = {
                        path for path in cls.universe if template.match_path(path) is not None
                    }

    def test_every_pair_against_enumeration(self):
        texts = list(self.matched)
        self.assertGreater(len(texts), 40)
        for left, right in itertools.combinations(texts, 2):
            pair = analyze_templates(left, right)
            left_set = self.matched[left]
            right_set = self.matched[right]
            self.assertEqual(
                pair.intersects,
                bool(left_set & right_set),
                f"intersection wrong for {left} vs {right}",
            )
            self.assertEqual(
                pair.a_subset_b,
                left_set <= right_set,
                f"a_subset_b wrong for {left} vs {right}",
            )
            self.assertEqual(
                pair.b_subset_a,
                right_set <= left_set,
                f"b_subset_a wrong for {left} vs {right}",
            )
            self.assertEqual(pair.equal, left_set == right_set)
            if pair.intersects:
                assert pair.witness_path is not None
                # The witness need not be drawn from the probe alphabet, so
                # check it against the templates themselves.
                self.assertIsNotNone(
                    parse_template(left).match_path(pair.witness_path),
                    f"{pair.witness_path} should match {left}",
                )
                self.assertIsNotNone(
                    parse_template(right).match_path(pair.witness_path),
                    f"{pair.witness_path} should match {right}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
