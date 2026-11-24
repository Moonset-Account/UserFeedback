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


def _request(**overrides) -> PublishRequest:
    defaults = dict(
        request_id="r",
        columns=("nx",),
        records={"s1": {"nx": "A"}, "s2": {"nx": "A"}},
        audience_groups=("g1",),
    )
    defaults.update(overrides)
    return PublishRequest(**defaults)


class ValidationTest(unittest.TestCase):
    def test_k_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                ReviewService([Member("a", ("g1",))], k=bad)

    def test_member_requires_non_empty_id_and_group(self):
        with self.assertRaises(ValueError):
            Member("", ("g1",))
        with self.assertRaises(ValueError):
            Member("a", ())
        member = Member("a", ("g2", "g1", "g1"))
        self.assertEqual(member.groups, ("g1", "g2"))

    def test_view_versions_must_be_contiguous(self):
        service = ReviewService([Member("a", ("g1",))], k=2)
        with self.assertRaises(ValueError):
            service.add_published_view(
                PublishedView(2, "g1", ("h",), {"s1": {"h": "x"}})
            )
        service.add_published_view(
            PublishedView(1, "g1", ("h",), {"s1": {"h": "x"}})
        )
        with self.assertRaises(ValueError):
            service.add_published_view(
                PublishedView(3, "g1", ("h",), {"s1": {"h": "x"}})
            )

    def test_view_row_cannot_reference_unknown_column(self):
        with self.assertRaises(ValueError):
            PublishedView(1, "g1", ("h",), {"s1": {"bogus": "x"}})

    def test_request_requires_records_and_matching_columns(self):
        with self.assertRaises(ValueError):
            _request(records={})
        with self.assertRaises(ValueError):
            _request(records={"s1": {"nx": "A"}, "s2": {"other": "B"}})
        with self.assertRaises(ValueError):
            _request(columns=("nx", "nx"), records={})

    def test_duplicate_request_id_with_changed_payload_is_an_error(self):
        service = ReviewService([Member("a", ("g1",))], k=2)
        service.preview(_request(request_id="dup", records={"s1": {"nx": "A"}, "s2": {"nx": "A"}}))
        with self.assertRaises(ValueError):
            service.preview(
                _request(request_id="dup", records={"s1": {"nx": "Z"}, "s2": {"nx": "Z"}})
            )

    def test_new_columns_must_not_shadow_visible_history_columns(self):
        service = ReviewService([Member("a", ("g1",))], k=2)
        service.add_published_view(
            PublishedView(
                1,
                "g1",
                ("nx",),
                {"s1": {"nx": "old"}, "s2": {"nx": "old"}},
            )
        )
        with self.assertRaises(ValueError):
            service.preview(_request())

    def test_to_dict_is_json_like_and_omits_subjects(self):
        service = ReviewService([Member("a", ("g1",))], k=3)
        result = service.preview(_request())
        payload = result.to_dict()
        self.assertEqual(payload["decision"], Decision.REJECT.value)
        self.assertEqual(payload["base_version"], 0)
        self.assertEqual(payload["k"], 3)
        self.assertEqual(payload["risks"][0]["audience_combination"], ["g1"])
        self.assertEqual(payload["risks"][0]["minimum_group_size"], 2)
        self.assertNotIn("s1", repr(payload))
        self.assertNotIn("s2", repr(payload))

    def test_view_with_no_relevant_history_treats_all_subjects_as_missing(self):
        # Member of g2 evaluating an audience restricted to g1: no g2 history
        # exists, so every subject shares the all-MISSING historical prefix.
        service = ReviewService(
            [Member("a", ("g1",)), Member("b", ("g2",))], k=2
        )
        result = service.preview(
            PublishRequest(
                request_id="no-history",
                columns=("nx",),
                records={
                    "s1": {"nx": "A"},
                    "s2": {"nx": "A"},
                    "s3": {"nx": "B"},
                },
                audience_groups=("g1",),
            )
        )
        # g1-only combination: classes of sizes 2 and 1 under k=2 -> reject;
        # g2 is not in the audience and therefore not evaluated at all.
        combos = {risk.audience_combination for risk in result.risks}
        self.assertEqual(combos, {("g1",)})
        self.assertEqual(result.minimum_group_size, 1)


if __name__ == "__main__":
    unittest.main()
