import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from privacy_review import (  # noqa: E402
    Decision,
    ReviewService,
    build_interleaving_scenario,
)


class VersionBindingTest(unittest.TestCase):
    def test_both_previews_pass_against_same_base_version(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        self.assertEqual(service.current_version, 2)

        preview_one = service.preview(scenario.first_request)
        preview_two = service.preview(scenario.second_request)

        self.assertEqual(preview_one.decision, Decision.ACCEPT)
        self.assertEqual(preview_two.decision, Decision.ACCEPT)
        # Each split yields pairs (size 2), exactly at k=2.
        self.assertEqual(preview_one.minimum_group_size, 2)
        self.assertEqual(preview_two.minimum_group_size, 2)
        self.assertEqual(preview_one.base_version, 2)
        self.assertEqual(preview_two.base_version, 2)
        # Previews do not mutate published state.
        self.assertEqual(service.current_version, 2)

    def test_first_publish_is_accepted_and_creates_views(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        preview_one = service.preview(scenario.first_request)

        publish_one = service.publish(scenario.first_request, preview_one)
        self.assertEqual(publish_one.decision, Decision.ACCEPT)
        self.assertEqual(publish_one.published_version, 3)
        self.assertEqual(service.current_version, 3)
        # One view per audience group at the new version.
        versions = [(view.version, view.group_id) for view in service.published_views]
        self.assertIn((3, "g1"), versions)
        self.assertIn((3, "g2"), versions)

    def test_second_publish_recomputes_against_latest_view_and_rejects(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        preview_one = service.preview(scenario.first_request)
        preview_two = service.preview(scenario.second_request)
        service.publish(scenario.first_request, preview_one)
        self.assertEqual(service.current_version, 3)

        publish_two = service.publish(scenario.second_request, preview_two)

        # The stale preview (base v2) triggers recomputation at v3; combined
        # columns form singleton classes, so the second release is rejected.
        self.assertEqual(publish_two.decision, Decision.REJECT)
        self.assertTrue(publish_two.recomputed)
        self.assertEqual(publish_two.base_version, 3)
        self.assertEqual(publish_two.minimum_group_size, 1)
        self.assertEqual(publish_two.published_version, None)
        # Rejection creates no new view.
        self.assertEqual(service.current_version, 3)
        # Every reachable combination is affected by the cross-product.
        combos = {risk.audience_combination for risk in publish_two.risks}
        self.assertEqual(combos, {("g1",), ("g2",), ("g1", "g2")})
        self.assertTrue(all(risk.minimum_group_size == 1 for risk in publish_two.risks))

    def test_repeated_publish_returns_stored_result_without_recomputing(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        preview_one = service.preview(scenario.first_request)
        first = service.publish(scenario.first_request, preview_one)
        # Re-submitting the identical request yields the exact stored outcome.
        again = service.publish(scenario.first_request, preview_one)
        self.assertIs(again, first)
        self.assertEqual(again.published_version, 3)
        # No extra view was created.
        self.assertEqual(
            [view.version for view in service.published_views], [1, 2, 3, 3]
        )

    def test_repeated_preview_returns_first_result_even_after_views_advance(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        first_preview = service.preview(scenario.first_request)
        service.publish(
            scenario.first_request,
            service.preview(scenario.first_request),
        )
        self.assertEqual(service.current_version, 3)

        # Same request_id again: the original v2-bound preview is returned,
        # not a fresh evaluation against v3.
        cached = service.preview(scenario.first_request)
        self.assertIs(cached, first_preview)
        self.assertEqual(cached.base_version, 2)
        self.assertEqual(cached.decision, Decision.ACCEPT)

    def test_publish_without_preview_evaluates_current_version(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        # First request published directly (no preview object).
        direct = service.publish(scenario.first_request)
        self.assertEqual(direct.decision, Decision.ACCEPT)
        self.assertEqual(direct.base_version, 2)
        self.assertFalse(direct.recomputed)
        self.assertEqual(service.current_version, 3)

        # Second request, also direct, must now fail on the latest view.
        direct_two = service.publish(scenario.second_request)
        self.assertEqual(direct_two.decision, Decision.REJECT)
        self.assertEqual(direct_two.minimum_group_size, 1)
        self.assertEqual(service.current_version, 3)

    def test_current_preview_is_honoured_without_recomputation(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        preview_two = service.preview(scenario.second_request)
        # Nothing published in between: base version still matches.
        publish_two = service.publish(scenario.second_request, preview_two)
        self.assertFalse(publish_two.recomputed)
        self.assertEqual(publish_two.decision, Decision.ACCEPT)
        self.assertEqual(publish_two.published_version, 3)

    def test_rejected_publish_can_still_be_found_on_resubmission(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        preview_one = service.preview(scenario.first_request)
        preview_two = service.preview(scenario.second_request)
        service.publish(scenario.first_request, preview_one)
        rejected = service.publish(scenario.second_request, preview_two)
        self.assertEqual(rejected.decision, Decision.REJECT)

        found_again = service.publish(scenario.second_request, preview_two)
        self.assertIs(found_again, rejected)
        self.assertEqual(service.current_version, 3)

    def test_preview_from_other_request_id_is_rejected(self):
        scenario = build_interleaving_scenario()
        service = scenario.service
        preview_one = service.preview(scenario.first_request)
        with self.assertRaises(ValueError):
            service.publish(scenario.second_request, preview_one)


if __name__ == "__main__":
    unittest.main()
