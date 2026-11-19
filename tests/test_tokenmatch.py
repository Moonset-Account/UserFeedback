"""Acceptance tests for tokenmatch.

Coverage of the cross-constraints in the task statement:

* accepted result: one-to-one renamed local bindings across a full fragment;
* blocked/rejected result: inconsistent renames, non-injective mappings,
  external-name and literal mismatches, scope-kind mismatches;
* boundary/interleaving: min_length off-by-one, boilerplate that severs two
  fragments so they cannot be joined to reach the threshold, declarations
  outside the candidate fragment still verified by input binding number, and
  multiple diagonals.
"""

import os
import sys
import unittest

# Make the workspace root importable under ``python -I`` (isolated mode
# ignores PYTHONPATH and does not add the CWD in every invocation style).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenmatch import (
    Boilerplate,
    LITERAL,
    NAME_BIND,
    NAME_REF,
    SCOPE_ENTER,
    SCOPE_EXIT,
    Token,
    compare_modules,
    compatible,
    literal,
    name_bind,
    name_ref,
    scope_enter,
    scope_exit,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def renamed_fragment():
    """Two structurally identical fragments with renamed local bindings."""
    left = [
        scope_enter(0, "function", "def"),
        name_bind(1, "foo", 1),
        scope_enter(2, "block", ":"),
        name_bind(3, "x", 2),
        name_ref(4, "x", 2),
        name_ref(5, "foo", 1),
        scope_exit(6, "block"),
        scope_exit(7, "function"),
    ]
    right = [
        scope_enter(0, "function", "def"),
        name_bind(1, "bar", 11),
        scope_enter(2, "block", ":"),
        name_bind(3, "y", 12),
        name_ref(4, "y", 12),
        name_ref(5, "bar", 11),
        scope_exit(6, "block"),
        scope_exit(7, "function"),
    ]
    return left, right


def main_diagonal(matches):
    """Keep only matches lying on the diagonal ``left_index == right_index``.

    At ``min_length=1`` every other anti-diagonal also carries a valid
    maximal match of length 1; filtering on the invariant difference (not the
    start values, which coincide trivially for length-1 matches) isolates the
    intended alignment.
    """
    return [m for m in matches if m.left_start - m.right_start == 0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class AcceptedMatchTests(unittest.TestCase):
    def test_full_renamed_fragment_is_one_match_with_bijection(self):
        left, right = renamed_fragment()
        matches = compare_modules(left, right, min_length=8)

        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual((m.left_start, m.left_end), (0, 8))
        self.assertEqual((m.right_start, m.right_end), (0, 8))
        self.assertEqual(m.length, 8)
        self.assertEqual(m.bindings, {1: 11, 2: 12})
        self.assertEqual(
            m.pairs(), [(k, k) for k in range(8)]
        )

    def test_reused_binding_does_not_grow_mapping(self):
        left, right = renamed_fragment()
        # References repeat bindings 1 and 2; mapping must remain two pairs.
        # Other diagonals also yield short maximal matches at min_length=1, so
        # inspect the main diagonal where the full fragment aligns.
        matches = main_diagonal(compare_modules(left, right, min_length=1))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].bindings, {1: 11, 2: 12})


class BlockedMatchTests(unittest.TestCase):
    def test_inconsistent_rename_of_same_local_binding_is_blocked(self):
        # Left binding 2 is first aligned with right binding 12, then with 13.
        left = [
            name_ref(0, "x", 2),
            name_ref(1, "x", 2),
            name_ref(2, "x", 2),
        ]
        right = [
            name_ref(0, "y", 12),
            name_ref(1, "z", 13),
            name_ref(2, "y", 12),
        ]
        # No run reaches length 3: the conflict at index 1 splits the stream.
        self.assertEqual(compare_modules(left, right, min_length=3), [])

        matches = main_diagonal(compare_modules(left, right, min_length=1))
        # The main diagonal splits into three maximal length-1 pieces; the
        # conflicting middle pair carries the alternative mapping on its own.
        self.assertEqual(len(matches), 3)
        self.assertEqual(
            [(m.left_start, m.left_end) for m in matches],
            [(0, 1), (1, 2), (2, 3)],
        )
        self.assertEqual(
            [m.bindings for m in matches],
            [{2: 12}, {2: 13}, {2: 12}],
        )

    def test_non_injective_two_locals_to_one_binding_is_blocked(self):
        left = [name_ref(0, "x", 1), name_ref(1, "y", 2)]
        right = [name_ref(0, "a", 9), name_ref(1, "a", 9)]
        self.assertEqual(compare_modules(left, right, min_length=2), [])
        # On the main diagonal each left binding survives alone; they can
        # never be joined because both would point at right binding 9.
        matches = main_diagonal(compare_modules(left, right, min_length=1))
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(m.length == 1 for m in matches))
        self.assertEqual([m.bindings for m in matches], [{1: 9}, {2: 9}])

    def test_external_names_match_by_spelling_only(self):
        left = [
            name_ref(0, "print", None, external=True),
            name_ref(1, "len", None, external=True),
        ]
        right_same = [
            name_ref(0, "print", None, external=True),
            name_ref(0, "len", None, external=True),
        ]
        self.assertEqual(
            len(compare_modules(left, right_same, min_length=2)), 1
        )

        right_renamed = [
            name_ref(0, "emit", None, external=True),
            name_ref(1, "len", None, external=True),
        ]
        # First pair incompatible -> only the second pair matches.
        matches = compare_modules(left, right_renamed, min_length=1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].left_start, 1)
        self.assertEqual(matches[0].bindings, {})

        # Local vs external pairing is rejected outright.
        ok, is_local = compatible(
            name_ref(0, "x", 5), name_ref(0, "x", None, external=True)
        )
        self.assertFalse(ok)
        self.assertFalse(is_local)

    def test_literals_match_by_value_and_block_on_mismatch(self):
        left = [literal(0, 1), literal(1, "s"), literal(2, 2)]
        right = [literal(0, 1), literal(1, "t"), literal(2, 2)]
        self.assertEqual(compare_modules(left, right, min_length=2), [])
        matches = compare_modules(left, right, min_length=1)
        self.assertEqual({m.left_start for m in matches}, {0, 2})

        # Equal values with different surface spellings still match.
        ok, _ = compatible(literal(0, 1, "0x1"), literal(0, 1, "1"))
        self.assertTrue(ok)

    def test_scope_kind_mismatch_severs_run(self):
        left = [
            scope_enter(0, "function"),
            name_ref(1, "x", 1),
        ]
        right = [
            scope_enter(0, "class"),
            name_ref(1, "x", 1),
        ]
        matches = compare_modules(left, right, min_length=1)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].left_start, 1)
        self.assertEqual(matches[0].length, 1)


class BoundaryAndInterleavingTests(unittest.TestCase):
    def test_min_length_boundary_is_inclusive(self):
        left, right = renamed_fragment()
        self.assertEqual(len(compare_modules(left, right, min_length=8)), 1)
        self.assertEqual(compare_modules(left, right, min_length=9), [])

    def test_boilerplate_severs_and_cannot_join_to_threshold(self):
        # 3 real tokens, 2 boilerplate tokens, 3 real tokens: without the
        # barrier the diagonal would look like an 8-token contiguous run.
        left = [
            name_ref(0, "a", 1),
            name_ref(1, "b", 2),
            name_ref(2, "c", 3),
            literal(3, "BOILER"),
            literal(4, "BOILER"),
            name_ref(5, "d", 4),
            name_ref(6, "e", 5),
            name_ref(7, "f", 6),
        ]
        right = [
            name_ref(0, "a2", 10),
            name_ref(1, "b2", 11),
            name_ref(2, "c2", 12),
            literal(3, "BOILER"),
            literal(4, "BOILER"),
            name_ref(5, "d2", 13),
            name_ref(6, "e2", 14),
            name_ref(7, "f2", 15),
        ]
        spans = [
            Boilerplate("left", 3, 5),
            Boilerplate("right", 3, 5),
        ]
        # Threshold 4 cannot be reached through the boilerplate.
        self.assertEqual(
            compare_modules(left, right, min_length=4, boilerplate=spans), []
        )
        matches = main_diagonal(
            compare_modules(left, right, min_length=3, boilerplate=spans)
        )
        self.assertEqual(len(matches), 2)
        self.assertEqual(
            [(m.left_start, m.left_end) for m in matches], [(0, 3), (5, 8)]
        )
        # The two surviving matches carry disjoint binding bijections.
        self.assertEqual(matches[0].bindings, {1: 10, 2: 11, 3: 12})
        self.assertEqual(matches[1].bindings, {4: 13, 5: 14, 6: 15})

        # Without marking the boilerplate, the same stream joins into one run.
        joined = compare_modules(left, right, min_length=4)
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].length, 8)

    def test_one_sided_boilerplate_already_severs(self):
        left = [
            name_ref(0, "a", 1),
            literal(1, "BOILER"),
            name_ref(2, "b", 2),
        ]
        right = [
            name_ref(0, "a", 10),
            literal(1, "BOILER"),
            name_ref(2, "b", 11),
        ]
        spans = [Boilerplate("left", 1, 2)]
        self.assertEqual(
            compare_modules(left, right, min_length=3, boilerplate=spans), []
        )
        matches = compare_modules(left, right, min_length=1, boilerplate=spans)
        self.assertEqual({m.left_start for m in matches}, {0, 2})

    def test_declaration_outside_fragment_verified_by_input_binding(self):
        # Binding 1 is declared at a position outside the matched interval
        # (the left-side declaration occupies an extra prefix slot, so no
        # token pair covers it).  The fragment only contains references, which
        # must still resolve to the outside declaration by binding number.
        left = [
            name_bind(0, "helper", 1),
            name_ref(1, "helper", 1),
            name_ref(2, "helper", 1),
            name_ref(3, "helper", 1),
        ]
        right = [
            literal(0, 0),
            name_ref(1, "util", 7),
            name_ref(2, "util", 7),
            name_ref(3, "util", 7),
        ]
        # The declaration (binding 7) is not present in the right stream at
        # all; references carry the input binding number anyway.
        matches = compare_modules(left, right, min_length=3)
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual((m.left_start, m.left_end), (1, 4))
        self.assertEqual((m.right_start, m.right_end), (1, 4))
        self.assertEqual(m.bindings, {1: 7})

        # A reference inside the fragment pointing at a different declaration
        # breaks the match even though the spelling is identical.
        right_drifted = [
            literal(0, 0),
            name_ref(1, "util", 7),
            name_ref(2, "util", 8),
            name_ref(3, "util", 7),
        ]
        self.assertEqual(
            compare_modules(left, right_drifted, min_length=3), []
        )

    def test_conflict_restart_keeps_maximal_tail_and_mapping(self):
        # Conflict at index 2; the run restarts after the latest occurrence of
        # either conflicting binding (index 1) and grows again.
        left = [
            name_ref(0, "x", 1),
            name_ref(1, "y", 2),
            name_ref(2, "x", 1),
            name_ref(3, "z", 3),
        ]
        right = [
            name_ref(0, "a", 10),
            name_ref(1, "b", 11),
            name_ref(2, "b", 11),  # would map 1 -> 11, collides with 2 -> 11
            name_ref(3, "c", 12),
        ]
        matches = main_diagonal(compare_modules(left, right, min_length=2))
        # Prefix [0,2) reaches length 2; restarted run [2,4) reaches length 2.
        self.assertEqual(len(matches), 2)
        self.assertEqual(
            [(m.left_start, m.left_end) for m in matches], [(0, 2), (2, 4)]
        )
        self.assertEqual(matches[1].bindings, {1: 11, 3: 12})

    def test_multiple_diagonals_report_independent_maximal_matches(self):
        # Shared tail (x x) appears at left offset 1 and right offset 0.
        left = [
            literal(0, 0),
            name_ref(1, "x", 1),
            name_ref(2, "x", 1),
        ]
        right = [
            name_ref(0, "p", 8),
            name_ref(1, "p", 8),
        ]
        matches = compare_modules(left, right, min_length=2)
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual((m.left_start, m.left_end), (1, 3))
        self.assertEqual((m.right_start, m.right_end), (0, 2))
        self.assertEqual(m.bindings, {1: 8})


class InputValidationTests(unittest.TestCase):
    def test_min_length_below_one_rejected(self):
        with self.assertRaises(ValueError):
            compare_modules([], [], min_length=0)

    def test_empty_streams(self):
        self.assertEqual(compare_modules([], [], min_length=1), [])

    def test_token_constructor_validates_kind_and_binding(self):
        with self.assertRaises(ValueError):
            Token(index=0, kind="keyword")
        with self.assertRaises(ValueError):
            Token(index=-1, kind=LITERAL)
        with self.assertRaises(ValueError):
            name_bind(0, "x", None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            name_ref(0, "x", None)

    def test_boilerplate_validation(self):
        with self.assertRaises(ValueError):
            Boilerplate("middle", 0, 1)
        with self.assertRaises(ValueError):
            Boilerplate("left", 2, 1)

    def test_kind_constants_cover_all_annotations(self):
        from tokenmatch.tokens import _KINDS

        self.assertEqual(
            _KINDS,
            {SCOPE_ENTER, SCOPE_EXIT, NAME_BIND, NAME_REF, LITERAL},
        )
        self.assertEqual(len(_KINDS), 5)

    def test_tokens_are_value_objects(self):
        a = name_ref(0, "x", 3)
        b = name_ref(0, "x", 3)
        self.assertEqual(a, b)
        with self.assertRaises(Exception):
            a.binding = 4  # type: ignore[misc]


class RandomizedInvariantTests(unittest.TestCase):
    """Cross-check every retained match against the stated constraints."""

    def _random_streams(self, seed, size=24):
        import random

        rng = random.Random(seed)
        left_bindings = list(range(1, 6))
        right_bindings = list(range(10, 15))

        def make(rng, bindings, names):
            stream = []
            for i in range(size):
                roll = rng.random()
                if roll < 0.55:
                    b = rng.choice(bindings)
                    if rng.random() < 0.3:
                        stream.append(name_bind(i, rng.choice(names), b))
                    else:
                        stream.append(name_ref(i, rng.choice(names), b))
                elif roll < 0.75:
                    stream.append(literal(i, rng.choice([0, 1, "s", "t"])))
                elif roll < 0.9:
                    stream.append(
                        name_ref(i, rng.choice(["print", "len"]), None,
                                 external=True)
                    )
                else:
                    from tokenmatch import scope_exit

                    flavor = rng.choice(["function", "block"])
                    stream.append(
                        scope_enter(i, flavor)
                        if rng.random() < 0.5
                        else scope_exit(i, flavor)
                    )
            return stream

        return (
            make(rng, left_bindings, ["a", "b", "c", "d"]),
            make(rng, right_bindings, ["p", "q", "r", "s"]),
        )

    def test_invariants_and_maximality_across_seeds(self):
        for seed in range(12):
            left, right = self._random_streams(seed)
            min_length = 2
            boiler = [
                Boilerplate("left", 8, 10),
                Boilerplate("right", 8, 10),
            ]
            left_mark = {
                i
                for b in boiler
                if b.side == "left"
                for i in range(b.start, b.end)
            }
            right_mark = {
                i
                for b in boiler
                if b.side == "right"
                for i in range(b.start, b.end)
            }

            matches = compare_modules(
                left, right, min_length=min_length, boilerplate=boiler
            )

            for m in matches:
                # Threshold and equal-span geometry.
                self.assertGreaterEqual(m.length, min_length)
                self.assertEqual(m.left_end - m.left_start, m.length)
                self.assertEqual(m.right_end - m.right_start, m.length)

                # Boilerplate never participates.
                self.assertFalse(
                    any(i in left_mark
                        for i in range(m.left_start, m.left_end))
                )
                self.assertFalse(
                    any(j in right_mark
                        for j in range(m.right_start, m.right_end))
                )

                # Rebuild the bijection from actual pairs and verify every
                # pair is locally compatible with the reported mapping.
                l_to_r: dict[int, int] = {}
                r_to_l: dict[int, int] = {}
                for k in range(m.length):
                    lt = left[m.left_start + k]
                    rt = right[m.right_start + k]
                    ok, is_local = compatible(lt, rt)
                    self.assertTrue(ok)
                    if is_local:
                        lb, rb = lt.binding, rt.binding
                        self.assertIn(lb, m.bindings)
                        self.assertEqual(m.bindings[lb], rb)
                        if lb in l_to_r:
                            self.assertEqual(l_to_r[lb], rb)
                        else:
                            self.assertNotIn(rb, r_to_l)
                            l_to_r[lb] = rb
                            r_to_l[rb] = lb
                self.assertEqual(l_to_r, m.bindings)

                # Maximality: extending at either end must be impossible.
                # A candidate neighbor is rejected if it is incompatible,
                # boilerplate, or would break the match's binding bijection.
                def can_extend(lt, rt):
                    ok, is_local = compatible(lt, rt)
                    if not ok:
                        return False
                    if is_local:
                        lb, rb = lt.binding, rt.binding
                        if lb in l_to_r and l_to_r[lb] != rb:
                            return False
                        if rb in r_to_l and r_to_l[rb] != lb:
                            return False
                    return True

                if m.left_start > 0 and m.right_start > 0:
                    self.assertFalse(
                        can_extend(
                            left[m.left_start - 1], right[m.right_start - 1]
                        )
                        and (m.left_start - 1) not in left_mark
                        and (m.right_start - 1) not in right_mark
                    )
                if m.left_end < len(left) and m.right_end < len(right):
                    self.assertFalse(
                        can_extend(left[m.left_end], right[m.right_end])
                        and m.left_end not in left_mark
                        and m.right_end not in right_mark
                    )

    def test_boilerplate_guarantee_no_cross_join(self):
        left, right = self._random_streams(seed=4, size=20)
        boiler = [Boilerplate("left", 5, 9), Boilerplate("right", 5, 9)]
        matches = compare_modules(
            left, right, min_length=1, boilerplate=boiler
        )
        for m in matches:
            spans_boiler_left = m.left_start < 9 and m.left_end > 5
            spans_boiler_right = m.right_start < 9 and m.right_end > 5
            self.assertFalse(spans_boiler_left and spans_boiler_right)


if __name__ == "__main__":
    unittest.main(verbosity=2)
