import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solution import Partition, prepare_join  # noqa: E402


def coordinate_pairs(matches):
    return [(m.left.coordinate, m.right.coordinate) for m in matches]


class TextEquivalenceTests(unittest.TestCase):
    def setUp(self):
        # Codes intentionally restart at zero in every partition.
        self.left = [
            Partition(["a", "b"], [0, 1, 0], [1, 2, 1]),
            Partition(["b", "c"], [0, 1, 0], [2, 3, 9]),
        ]
        self.right = [
            Partition(["B", "a"], [0, 1, 1], [2, 1, 1]),
            Partition(["a", "z"], [0, 0], [1, 7]),
        ]

    def test_exact_uses_cross_partition_dictionary_identity(self):
        plan = prepare_join(self.left, self.right)
        matches = plan.fetch_all()

        # a/1: left (0,0),(0,2); right (0,1),(0,2),(1,0).
        # b/2 does not match uppercase B/2 in exact mode.
        expected = []
        for left_row in ((0, 0), (0, 2)):
            for right_row in ((0, 1), (0, 2), (1, 0)):
                expected.append((left_row, right_row))

        self.assertEqual(coordinate_pairs(matches), expected)
        self.assertEqual(plan.total_matches, 6)

        exact_a = [
            cls for cls in plan.text_classes if "a" in cls.raw_values
        ]
        self.assertEqual(len(exact_a), 1)
        refs = exact_a[0].references
        self.assertIn(("left", 0, 0), refs)
        self.assertIn(("right", 0, 1), refs)
        self.assertIn(("right", 1, 0), refs)

    def test_casefold_connects_distinct_partition_local_codes(self):
        plan = prepare_join(
            self.left, self.right, text_comparison="unicode_casefold"
        )
        matches = plan.fetch_all()

        expected = [
            ((0, 0), (0, 1)),
            ((0, 0), (0, 2)),
            ((0, 0), (1, 0)),
            ((0, 1), (0, 0)),  # b == B after folding, both int key 2
            ((0, 2), (0, 1)),
            ((0, 2), (0, 2)),
            ((0, 2), (1, 0)),
            ((1, 0), (0, 0)),  # second left partition's code zero is b
        ]
        self.assertEqual(coordinate_pairs(matches), expected)
        self.assertEqual(plan.total_matches, 8)

        b_class = next(
            cls for cls in plan.text_classes if cls.normalized_key == "b"
        )
        self.assertEqual(
            set(b_class.raw_values), {"b", "B"}
        )
        self.assertIn(("left", 0, 1), b_class.references)
        self.assertIn(("left", 1, 0), b_class.references)
        self.assertIn(("right", 0, 0), b_class.references)

    def test_casefold_distinct_dictionary_entries_keep_all_row_groups(self):
        left = [
            Partition(
                ["Straße", "STRASSE"],
                [0, 1],
                [7, 7],
                partition_id="L",
            )
        ]
        right = [
            Partition(
                ["strasse", "Strasse"],
                [0, 1],
                [7, 7],
                partition_id="R",
            )
        ]

        exact = prepare_join(left, right, text_comparison="exact").fetch_all()
        self.assertEqual(exact, ())

        folded_plan = prepare_join(left, right, text_comparison="casefold")
        folded = folded_plan.fetch_all()

        # Two left dictionary groups times two right dictionary groups.
        self.assertEqual(len(folded), 4)
        self.assertEqual(
            coordinate_pairs(folded),
            [
                ((0, 0), (0, 0)),
                ((0, 0), (0, 1)),
                ((0, 1), (0, 0)),
                ((0, 1), (0, 1)),
            ],
        )
        klass = folded_plan.text_classes[0]
        self.assertEqual(klass.normalized_key, "strasse")
        self.assertEqual(
            set(klass.raw_values),
            {"Straße", "STRASSE", "strasse", "Strasse"},
        )

    def test_kelvin_sign_uses_unicode_casefold_not_ascii_lower(self):
        kelvin = "K"  # KELVIN SIGN
        self.assertEqual(kelvin.casefold(), "k")

        left = [Partition(["k"], [0, 0], [1, 1])]
        right = [Partition(["K", kelvin], [0, 1, 0], [1, 1, 2])]

        folded = prepare_join(left, right, text_comparison="casefold").fetch_all()
        self.assertEqual(
            coordinate_pairs(folded),
            [((0, 0), (0, 0)), ((0, 0), (0, 1)), ((0, 1), (0, 0)), ((0, 1), (0, 1))],
        )

    def test_exact_mode_does_not_casefold_or_normalize(self):
        nfc = "café"
        nfd = "café"
        self.assertNotEqual(nfc, nfd)

        left = [Partition([nfc], [0], [1])]
        right = [Partition([nfd, nfc.upper()], [0, 1], [1, 1])]

        exact = prepare_join(left, right).fetch_all()
        self.assertEqual(exact, ())

        # Case folding handles case but deliberately does not perform Unicode
        # normalization, so NFC and NFD remain different code-point sequences.
        folded = prepare_join(left, right, text_comparison="casefold").fetch_all()
        self.assertEqual(coordinate_pairs(folded), [((0, 0), (0, 1))])


if __name__ == "__main__":
    unittest.main()
