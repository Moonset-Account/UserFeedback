"""区间端点（含开闭）、无界区间与三态判定测试。"""

import unittest

from adjudicator.interval import Interval, IntervalState
from adjudicator.versioning import parse_version


def state(interval: Interval, version_text: str) -> IntervalState:
    return interval.contains(parse_version(version_text))


class IntervalEndpointTest(unittest.TestCase):
    def test_default_brackets_match_osv_convention(self):
        # 缺省：下界闭、上界开
        interval = Interval(lower="1.2.0", upper="1.2.7")
        self.assertEqual(state(interval, "1.2.0"), IntervalState.INSIDE)
        self.assertEqual(state(interval, "1.2.7"), IntervalState.OUTSIDE)

    def test_explicit_closed_upper_endpoint(self):
        interval = Interval(lower="1.2.7", upper="1.2.10",
                            lower_inclusive=True, upper_inclusive=True)
        self.assertEqual(state(interval, "1.2.7"), IntervalState.INSIDE)
        self.assertEqual(state(interval, "1.2.10"), IntervalState.INSIDE)
        self.assertEqual(state(interval, "1.2.11"), IntervalState.OUTSIDE)

    def test_open_lower_endpoint(self):
        interval = Interval(lower="1.0.0", upper="2.0.0",
                            lower_inclusive=False, upper_inclusive=False)
        self.assertEqual(state(interval, "1.0.0"), IntervalState.OUTSIDE)
        self.assertEqual(state(interval, "1.0.1"), IntervalState.INSIDE)

    def test_prerelease_at_boundary(self):
        # (-inf, 2.0.0)：2.0.0-rc.1 在 2.0.0 之内，正式版 2.0.0 被开区间排除
        interval = Interval(lower=None, upper="2.0.0",
                            lower_inclusive=True, upper_inclusive=False)
        self.assertEqual(state(interval, "2.0.0-rc.1"), IntervalState.INSIDE)
        self.assertEqual(state(interval, "2.0.0"), IntervalState.OUTSIDE)
        self.assertEqual(state(interval, "0.0.1"), IntervalState.INSIDE)

    def test_fully_unbounded_interval(self):
        interval = Interval(lower=None, upper=None)
        self.assertEqual(state(interval, "0.0.1"), IntervalState.INSIDE)
        self.assertEqual(state(interval, "99.99.99-rc.9"),
                         IntervalState.INSIDE)

    def test_only_lower_bound(self):
        interval = Interval(lower="4.0.0", upper=None,
                            lower_inclusive=True)
        self.assertEqual(state(interval, "3.99.99"), IntervalState.OUTSIDE)
        self.assertEqual(state(interval, "4.0.0"), IntervalState.INSIDE)
        self.assertEqual(state(interval, "5.0.0-beta.1"), IntervalState.INSIDE)

    def test_unparseable_component_version_is_unknown(self):
        interval = Interval(lower="1.0.0", upper="2.0.0")
        self.assertEqual(interval.contains(parse_version("20261109-r1")),
                         IntervalState.UNKNOWN)
        self.assertEqual(interval.contains(None), IntervalState.UNKNOWN)

    def test_unparseable_endpoint_makes_interval_unknown(self):
        interval = Interval(lower="1.0", upper="2.0.0")
        self.assertFalse(interval.well_formed)
        self.assertEqual(interval.contains(parse_version("1.5.0")),
                         IntervalState.UNKNOWN)
        self.assertIn(("lower", "1.0"), interval.unparseable_endpoints)

    def test_inverted_bounds_rejected(self):
        with self.assertRaises(ValueError):
            Interval(lower="2.0.0", upper="1.0.0")

    def test_from_dict_and_describe(self):
        interval = Interval.from_dict(
            {"lower": "1.0.0", "upper": "2.0.0",
             "lower_inclusive": False, "upper_inclusive": True}
        )
        self.assertEqual(interval.describe(), "(1.0.0, 2.0.0]")


if __name__ == "__main__":
    unittest.main()
