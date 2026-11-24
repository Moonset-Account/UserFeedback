import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)


class PathTest(unittest.TestCase):
    def test_package_importable_under_isolated_mode(self):
        import build_semantics

        self.assertTrue(hasattr(build_semantics, "analyze"))


if __name__ == "__main__":
    unittest.main()
