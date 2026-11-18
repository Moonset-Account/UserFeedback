"""Acceptance tests for normalized query identifiers and the adapter."""

import math
import random
import sys
import unittest
from pathlib import Path

# Make the package importable when this file is run directly with -I.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from result_publisher import (  # noqa: E402
    DataSnapshot,
    LaplaceCountAdapter,
    Query,
    canonical_query_id,
)


class CanonicalIdentifierTest(unittest.TestCase):
    def test_equivalent_filters_share_identifier(self):
        left = Query(
            dataset="hr",
            purpose="staffing_audit",
            kind="on_duty_count",
            filters={"team": {"a", "b"}, "shift": "day"},
        )
        right = Query(
            dataset="hr",
            purpose="staffing_audit",
            kind="on_duty_count",
            # Different key order, set written as a list: same semantics.
            filters={"shift": "day", "team": ["b", "a"]},
        )
        self.assertEqual(left.canonical_id, right.canonical_id)

    def test_distinct_semantics_get_distinct_identifiers(self):
        base = dict(dataset="hr", purpose="staffing_audit", kind="on_duty_count")
        other_purpose = canonical_query_id(**base, filters={"shift": "day"})
        other_kind = canonical_query_id(
            dataset="hr", purpose="staffing_audit", kind="avg_hours",
            filters={"shift": "day"},
        )
        other_filter = canonical_query_id(**base, filters={"shift": "night"})
        self.assertNotEqual(other_purpose, other_kind)
        self.assertNotEqual(other_purpose, other_filter)

    def test_nested_and_tuple_inputs_are_normalized(self):
        first = canonical_query_id(
            dataset="hr",
            purpose="p",
            kind="k",
            filters={"loc": (3, 1, 2), "meta": {"z": 1, "a": 2}},
        )
        second = canonical_query_id(
            dataset="hr",
            purpose="p",
            kind="k",
            filters={"meta": {"a": 2, "z": 1}, "loc": [3, 1, 2]},
        )
        self.assertEqual(first, second)

    def test_blank_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            canonical_query_id(dataset="", purpose="p", kind="k", filters={})


class LaplaceCountAdapterTest(unittest.TestCase):
    def test_noise_centers_on_true_count_and_is_deterministic_with_seed(self):
        snapshot = DataSnapshot(
            dataset="hr", snapshot_id="s1", payload=[{"id": i} for i in range(10)]
        )
        query = Query("hr", "p", "on_duty_count", {"shift": "day"})
        first = LaplaceCountAdapter(rng=random.Random(42))
        second = LaplaceCountAdapter(rng=random.Random(42))
        self.assertEqual(
            first.add_noise(query, snapshot), second.add_noise(query, snapshot)
        )

        rng = random.Random(7)
        adapter = LaplaceCountAdapter(sensitivity=1.0, epsilon=10.0, rng=rng)
        samples = [adapter.add_noise(query, snapshot) for _ in range(400)]
        # With epsilon=10 the noise scale is 0.1; the mean must stay close
        # to the true count of 10.
        self.assertAlmostEqual(sum(samples) / len(samples), 10.0, delta=0.1)

    def test_empty_snapshot_counts_zero(self):
        class FixedRng:
            """Returns one fixed uniform draw every time it is sampled."""

            def __init__(self, value: float) -> None:
                self._value = value
                self.samples = 0

            def random(self) -> float:
                self.samples += 1
                return self._value

        rng = FixedRng(0.75)  # upper half -> noise = -ln(0.5) ~= 0.693
        adapter = LaplaceCountAdapter(
            sensitivity=1.0, epsilon=1.0, rng=rng  # type: ignore[arg-type]
        )
        query = Query("hr", "p", "on_duty_count")
        empty = adapter.add_noise(
            query, DataSnapshot("hr", "s-empty", payload=[])
        )
        missing = adapter.add_noise(
            query, DataSnapshot("hr", "s-none", payload=None)
        )
        # Both representations mean "zero rows", so each output is just
        # the identical deterministic noise term.
        self.assertAlmostEqual(empty, -math.log(0.5))
        self.assertAlmostEqual(missing, -math.log(0.5))
        self.assertEqual(rng.samples, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
