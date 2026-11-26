import json
import sys
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from capacity_audit import analyze  # noqa: E402


class ExampleFileTests(unittest.TestCase):
    def test_cross_window_example_file(self):
        path = ROOT / "examples" / "cross_window_reissue.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        result = analyze(
            data["events"],
            window_width=data["window_width"],
            exhaustion_at=data["exhaustion_at"],
            default_clock_bounds=data["default_clock_bounds"],
        )
        self.assertEqual(result.window(0).arrivals.point, 5)
        self.assertEqual(result.window(1).reissues.point, 4)
        self.assertEqual(result.window(1).completions.point, 3)
        self.assertEqual(result.window(1).withdrawals.point, 1)
        self.assertEqual(result.summary["capacity_lost_count"], 1)
        self.assertEqual(len(result.cross_window_reissues), 1)


if __name__ == "__main__":
    unittest.main()
