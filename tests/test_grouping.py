import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from privacy_review import (  # noqa: E402
    Decision,
    MISSING,
    Member,
    PublishedView,
    PublishRequest,
    ReviewService,
    build_missing_value_scenario,
)


def _service(k: int = 2) -> ReviewService:
    return ReviewService([Member("alice", ("g1",))], k=k)


class GroupingTest(unittest.TestCase):
    def test_combined_row_joins_history_and_new_columns(self):
        service = _service()
        service.add_published_view(
            PublishedView(
                1,
                "g1",
                ("hx",),
                {"s1": {"hx": "H1"}, "s2": {"hx": "H2"}},
            )
        )
        request = PublishRequest(
            request_id="r1",
            columns=("nx",),
            records={"s1": {"nx": "N"}, "s2": {"nx": "N"}},
            audience_groups=("g1",),
        )
        result = service.preview(request)
        # Distinct history values split the subjects even though the new
        # column is identical: classes {s1} and {s2}, each of size 1.
        self.assertEqual(result.decision, Decision.REJECT)
        self.assertEqual(result.minimum_group_size, 1)
        self.assertEqual(len(result.risks), 1)
        self.assertEqual(result.risks[0].audience_combination, ("g1",))
        self.assertEqual(result.risks[0].minimum_group_size, 1)

    def test_class_of_exactly_k_is_accepted(self):
        service = _service(k=2)
        service.add_published_view(
            PublishedView(1, "g1", ("hx",), {"s1": {"hx": "H"}, "s2": {"hx": "H"}})
        )
        request = PublishRequest(
            request_id="r-boundary",
            columns=("nx",),
            records={"s1": {"nx": "N"}, "s2": {"nx": "N"}},
            audience_groups=("g1",),
        )
        result = service.preview(request)
        self.assertEqual(result.decision, Decision.ACCEPT)
        self.assertEqual(result.minimum_group_size, 2)
        self.assertEqual(result.risks, ())

    def test_k_one_never_rejects_on_unique_classes(self):
        service = _service(k=1)
        request = PublishRequest(
            request_id="r-k1",
            columns=("nx",),
            records={
                "s1": {"nx": "A"},
                "s2": {"nx": "B"},
                "s3": {"nx": "C"},
            },
            audience_groups=("g1",),
        )
        result = service.preview(request)
        self.assertEqual(result.decision, Decision.ACCEPT)
        self.assertEqual(result.minimum_group_size, 1)

    def test_missing_history_value_is_a_visible_grouping_value_reject(self):
        scenario = build_missing_value_scenario()
        service = ReviewService([Member("alice", ("g1",))], k=2)
        for view in scenario.rejecting_history:
            service.add_published_view(view)
        result = service.preview(scenario.request)
        # Three subjects share H, one is uniquely MISSING -> class of size 1.
        self.assertEqual(result.decision, Decision.REJECT)
        self.assertEqual(result.minimum_group_size, 1)
        self.assertEqual(result.risks[0].minimum_group_size, 1)

    def test_shared_missing_class_reaches_k_and_is_accepted(self):
        scenario = build_missing_value_scenario()
        service = ReviewService([Member("alice", ("g1",))], k=2)
        for view in scenario.accepting_history:
            service.add_published_view(view)
        result = service.preview(scenario.request)
        # Classes: {s1,s2} on H and {s3,s4} on MISSING, both size 2.
        self.assertEqual(result.decision, Decision.ACCEPT)
        self.assertEqual(result.minimum_group_size, 2)

    def test_missing_sentinel_is_a_distinct_value(self):
        self.assertIs(MISSING, MISSING)
        self.assertEqual(hash(MISSING), hash(MISSING))
        self.assertNotEqual(MISSING, "H")
        self.assertTrue(bool(MISSING))

    def test_minimum_group_size_reports_smallest_of_many_classes(self):
        service = _service(k=3)
        service.add_published_view(
            PublishedView(
                1,
                "g1",
                ("hx",),
                {
                    "s1": {"hx": "A"},
                    "s2": {"hx": "A"},
                    "s3": {"hx": "A"},
                    "s4": {"hx": "B"},
                    "s5": {"hx": "B"},
                },
            )
        )
        request = PublishRequest(
            request_id="r-sizes",
            columns=("nx",),
            records={s: {"nx": "N"} for s in ("s1", "s2", "s3", "s4", "s5")},
            audience_groups=("g1",),
        )
        result = service.preview(request)
        self.assertEqual(result.decision, Decision.REJECT)
        # Classes of sizes 3 and 2; the smallest (2) is below k=3.
        self.assertEqual(result.minimum_group_size, 2)


if __name__ == "__main__":
    unittest.main()
