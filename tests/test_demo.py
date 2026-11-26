import io
import unittest
from contextlib import redirect_stdout

from frame_occlusion.demo import build_demo_output, main


class DemoTests(unittest.TestCase):
    def test_demo_output_shows_cross_screen_masks_and_pause_markers(self):
        output = build_demo_output()

        # Cross-screen ready scenario.
        self.assertIn("Cross-screen window", output)
        self.assertIn("....#", output)
        self.assertIn(".#.", output)
        self.assertIn(".O.", output)

        # Delayed-drag scenario and eventual release.
        self.assertIn("Drag frame v2 arrives", output)
        self.assertIn("..??", output)
        self.assertIn("??..", output)
        self.assertIn("..??|??..", output)
        self.assertIn("....|##..", output)
        self.assertIn("Publishable cells: 20/24", output)
        self.assertIn("##..", output)

        # Scaling/rotation are exercised/displayed.
        self.assertIn("scale=2", output)
        self.assertIn("rotated raw buffer", output)

    def test_main_prints_without_error(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            main()
        self.assertIn("Frame occlusion synthetic demo", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
