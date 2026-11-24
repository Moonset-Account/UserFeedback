import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from privacy_review import (  # noqa: E402
    Decision,
    Member,
    PublishedView,
    PublishRequest,
    ReviewService,
)


def _members() -> list[Member]:
    # Overlap: carol belongs to both g1 and g2; dave sits in an unrelated g3.
    return [
        Member("alice", ("g1",)),
        Member("bob", ("g2",)),
        Member("carol", ("g1", "g2")),
        Member("dave", ("g3",)),
    ]


def _request(request_id: str = "r-aud", **overrides) -> PublishRequest:
    defaults = dict(
        request_id=request_id,
        columns=("nx",),
        records={
            "s1": {"nx": "A"},
            "s2": {"nx": "A"},
            "s3": {"nx": "B"},
            "s4": {"nx": "B"},
        },
        audience_groups=("g1", "g2"),
    )
    defaults.update(overrides)
    return PublishRequest(**defaults)


class AudienceCombinationTest(unittest.TestCase):
    def test_each_reachable_combination_is_evaluated_separately(self):
        service = ReviewService(_members(), k=2)
        # g1 history already splits subjects into singleton classes for
        # g1-only viewers, while g2 history stays coarse.
        service.add_published_view(
            PublishedView(
                1,
                "g1",
                ("h1",),
                {
                    "s1": {"h1": "x"},
                    "s2": {"h1": "y"},
                    "s3": {"h1": "z"},
                    "s4": {"h1": "w"},
                },
            )
        )
        service.add_published_view(
            PublishedView(
                2,
                "g2",
                ("h2",),
                {s: {"h2": "H"} for s in ("s1", "s2", "s3", "s4")},
            )
        )
        result = service.preview(_request())
        self.assertEqual(result.decision, Decision.REJECT)
        # g1 and g1+g2 combinations inherit g1's fine-grained history; g2 alone
        # only sees the coarse history and the new pairs.
        risky = {risk.audience_combination: risk.minimum_group_size for risk in result.risks}
        self.assertIn(("g1",), risky)
        self.assertIn(("g1", "g2"), risky)
        self.assertNotIn(("g2",), risky)
        self.assertEqual(risky[("g1",)], 1)
        self.assertEqual(risky[("g1", "g2")], 1)

    def test_members_in_same_combination_are_not_double_reported(self):
        # Two distinct members both in g1 only: one combination, one finding.
        members = [Member("alice", ("g1",)), Member("erin", ("g1",))]
        service = ReviewService(members, k=3)
        result = service.preview(_request(audience_groups=("g1",)))
        self.assertEqual(len(result.risks), 1)
        self.assertEqual(result.risks[0].audience_combination, ("g1",))
        self.assertEqual(result.risks[0].minimum_group_size, 2)

    def test_unrelated_group_member_is_not_evaluated(self):
        service = ReviewService(_members(), k=5)
        # Only g3 exists in history; the audience is g1/g2 so dave (g3) is not
        # a viewer and g3's tiny classes must not influence the decision.
        service.add_published_view(
            PublishedView(1, "g3", ("h3",), {"s1": {"h3": "only"}})
        )
        result = service.preview(_request())
        # Viewers g1, g2, g1+g2 each see only the new column pairs (size 2);
        # k=5 makes those risky, but g3 never appears among the combinations.
        self.assertEqual(result.decision, Decision.REJECT)
        combos = {risk.audience_combination for risk in result.risks}
        self.assertNotIn(("g3",), combos)
        self.assertEqual(combos, {("g1",), ("g2",), ("g1", "g2")})

    def test_combination_members_see_union_of_group_views(self):
        service = ReviewService(_members(), k=2)
        service.add_published_view(
            PublishedView(1, "g1", ("h1",), {s: {"h1": "G"} for s in ("s1", "s2", "s3", "s4")})
        )
        service.add_published_view(
            PublishedView(2, "g2", ("h2",), {s: {"h2": "G"} for s in ("s1", "s2", "s3", "s4")})
        )
        result = service.preview(_request())
        # All combinations end in the two new-column pairs of size 2 -> accept.
        self.assertEqual(result.decision, Decision.ACCEPT)
        self.assertEqual(result.risks, ())

    def test_result_never_contains_subject_identifiers(self):
        service = ReviewService(_members(), k=4)
        result = service.preview(_request())
        rendered = repr(result.to_dict()) + repr(result.risks)
        for subject in ("s1", "s2", "s3", "s4"):
            self.assertNotIn(subject, rendered)

    def test_unknown_audience_group_is_rejected(self):
        service = ReviewService(_members(), k=2)
        with self.assertRaises(ValueError):
            service.preview(_request(audience_groups=("g1", "g9")))


if __name__ == "__main__":
    unittest.main()
