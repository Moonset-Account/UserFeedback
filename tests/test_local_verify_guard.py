"""Guard tests for tools/local_verify.py.

These prove the wrapper refuses to report success for a zero-test discovery,
an all-skipped suite or a failing suite — the exact cases where trusting
unittest's process exit code would silently pass.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

from tools import local_verify


class DiscoveryGuardTests(unittest.TestCase):
    # A per-test unique package name keeps each temporary fixture from
    # colliding with the real ``tests`` package or with a fixture imported by
    # a previous test in the same process (imports are cached in sys.modules).
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="verify_guard_")
        self.package = "verify_guard_" + self._testMethodName

    def tearDown(self):
        for name in [n for n in sys.modules if n.startswith(self.package)]:
            del sys.modules[name]
        shutil.rmtree(self.root, ignore_errors=True)

    def make_package(self, body):
        package_dir = os.path.join(self.root, self.package)
        os.makedirs(package_dir, exist_ok=True)
        with open(os.path.join(package_dir, "__init__.py"), "w",
                  encoding="utf-8") as handle:
            handle.write('"""Guard fixture package."""\n')
        with open(os.path.join(package_dir, "test_guard.py"), "w",
                  encoding="utf-8") as handle:
            handle.write(body)

    def run_guard(self):
        stream = io.StringIO()
        ok, evidence = local_verify.run_discovered_suite(
            os.path.join(self.root, self.package),
            self.root,
            stream=stream,
        )
        return ok, evidence, stream.getvalue()

    def test_empty_suite_is_rejected(self):
        self.make_package('"""No tests here."""\n')
        ok, evidence, output = self.run_guard()
        self.assertFalse(ok)
        self.assertIn("discovered=0", evidence)
        self.assertIn("discovered zero test cases", output)

    def test_all_skipped_suite_is_rejected(self):
        self.make_package(
            "import unittest\n"
            "\n"
            "class Skipped(unittest.TestCase):\n"
            "    @unittest.skip('deliberately skipped')\n"
            "    def test_skipped(self):\n"
            "        pass\n"
        )
        ok, evidence, _ = self.run_guard()
        self.assertFalse(ok)
        self.assertIn("executed=0", evidence)

    def test_failing_suite_is_rejected(self):
        self.make_package(
            "import unittest\n"
            "\n"
            "class Broken(unittest.TestCase):\n"
            "    def test_fails(self):\n"
            "        self.fail('intentional failure')\n"
        )
        ok, evidence, _ = self.run_guard()
        self.assertFalse(ok)
        self.assertIn("failures=1", evidence)

    def test_passing_suite_is_accepted(self):
        self.make_package(
            "import unittest\n"
            "\n"
            "class Healthy(unittest.TestCase):\n"
            "    def test_passes(self):\n"
            "        self.assertTrue(True)\n"
        )
        ok, evidence, output = self.run_guard()
        self.assertTrue(ok, evidence + "\n" + output)
        self.assertIn("executed=1", evidence)


if __name__ == "__main__":
    unittest.main()
