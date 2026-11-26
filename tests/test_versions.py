import unittest

from module_planner import Version, VersionParseError, VersionRange


class VersionRangeTests(unittest.TestCase):
    def test_comparator_interval_boundaries(self) -> None:
        interval = VersionRange.parse(">=1.2.0,<2.0.0")
        self.assertTrue(interval.contains("1.2.0"))
        self.assertTrue(interval.contains(Version.parse("1.9.9")))
        self.assertFalse(interval.contains("1.1.9"))
        self.assertFalse(interval.contains("2.0.0"))

    def test_mathematical_interval_inclusivity(self) -> None:
        closed_open = VersionRange.parse("[1.0.0,2.0.0)")
        self.assertTrue(closed_open.contains("1.0.0"))
        self.assertFalse(closed_open.contains("2.0.0"))

        open_closed = VersionRange.parse("(1.0.0,2.0.0]")
        self.assertFalse(open_closed.contains("1.0.0"))
        self.assertTrue(open_closed.contains("2.0.0"))

    def test_caret_and_tilde_ranges(self) -> None:
        self.assertTrue(VersionRange.parse("^1.2.3").contains("1.9.0"))
        self.assertFalse(VersionRange.parse("^1.2.3").contains("2.0.0"))
        self.assertTrue(VersionRange.parse("^0.2.3").contains("0.2.9"))
        self.assertFalse(VersionRange.parse("^0.2.3").contains("0.3.0"))
        self.assertTrue(VersionRange.parse("~1.2.3").contains("1.2.9"))
        self.assertFalse(VersionRange.parse("~1.2.3").contains("1.3.0"))

    def test_best_match_prefers_reuse_then_highest(self) -> None:
        versions = ["1.0.0", "1.4.0", "1.9.0", "2.0.0"]
        interval = VersionRange.parse(">=1.2,<2")
        self.assertEqual(interval.best(versions), Version.parse("1.9.0"))
        self.assertEqual(
            interval.best(versions, preferred="1.4.0"), Version.parse("1.4.0")
        )
        self.assertIsNone(VersionRange.parse(">=3").best(versions))

    def test_invalid_version_rejected(self) -> None:
        with self.assertRaises(VersionParseError):
            Version.parse("not-a-version")


if __name__ == "__main__":
    unittest.main()
