"""Path validation, collision-key and plan-stage tests.

Covers: CASE-PLAN-001, CASE-PATH-ESC-001, CASE-PATH-ABS-001,
CASE-PATH-BSL-001, CASE-COLL-CASE-001 / CASE-COLL-UNI-001 (key level).
"""

import os
import sys
import tempfile
import unicodedata
import unittest
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import helpers  # noqa: E402
from importer import (  # noqa: E402
    ERR_NAME_COLLISION,
    ERR_PATH_ABSOLUTE,
    ERR_PATH_BACKSLASH,
    ERR_PATH_EMPTY,
    ERR_PATH_ESCAPE,
    ImportFailure,
    collision_key,
    plan_entries,
    plan_report,
)


def plan_names(names, platform="darwin"):
    """Plan a synthetic archive made of empty file entries; return plans."""
    tmp = tempfile.mkdtemp()
    zpath = os.path.join(tmp, "fixture.zip")
    helpers.write_zip(zpath, [(n, b"", helpers.MODE_FILE) for n in names])
    with zipfile.ZipFile(zpath) as zf:
        return plan_entries(zf, platform=platform)


class TargetRelPathTests(unittest.TestCase):
    """CASE-PLAN-001: each entry's target relative path is listed first."""

    def test_plan_lists_target_relpaths(self):
        plans = plan_names(["docs/a.txt", "b.txt", "dir/"])
        self.assertEqual(
            [(p.name, p.rel_path, p.is_dir) for p in plans],
            [("docs/a.txt", "docs/a.txt", False),
             ("b.txt", "b.txt", False),
             ("dir/", "dir", True)])

    def test_plan_report_json_shape(self):
        tmp = tempfile.mkdtemp()
        zpath = os.path.join(tmp, "fixture.zip")
        helpers.write_zip(zpath, [("x/y.txt", b"hi", helpers.MODE_FILE)])
        report = plan_report(zpath, platform="darwin")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["entry_count"], 1)
        entry = report["entries"][0]
        self.assertEqual(entry["entry"], "x/y.txt")
        self.assertEqual(entry["target"], "x/y.txt")
        self.assertEqual(entry["kind"], "file")
        self.assertEqual(entry["declared_size"], 2)
        self.assertIn("collision_key", entry)

    def test_dot_components_collapsed(self):
        plans = plan_names(["a/./b.txt"])
        self.assertEqual(plans[0].rel_path, "a/b.txt")


class PathEscapeTests(unittest.TestCase):
    """CASE-PATH-ESC-001 / CASE-PATH-ABS-001 / CASE-PATH-BSL-001."""

    def assert_rejected(self, names, code):
        with self.assertRaises(ImportFailure) as ctx:
            plan_names(names)
        self.assertEqual(ctx.exception.code, code)
        self.assertIsNotNone(ctx.exception.entry)

    def test_dotdot_rejected(self):
        self.assert_rejected(["../evil.txt"], ERR_PATH_ESCAPE)

    def test_nested_dotdot_rejected(self):
        self.assert_rejected(["ok/../../evil.txt"], ERR_PATH_ESCAPE)

    def test_absolute_path_rejected(self):
        self.assert_rejected(["/etc/passwd"], ERR_PATH_ABSOLUTE)

    def test_drive_letter_rejected(self):
        self.assert_rejected(["C:/windows/evil.dll"], ERR_PATH_ABSOLUTE)

    def test_backslash_rejected(self):
        self.assert_rejected(["..\\evil.txt"], ERR_PATH_BACKSLASH)

    def test_nul_byte_rejected(self):
        # zipfile.ZipInfo truncates names at NUL, so the validator is
        # exercised directly here (defense in depth for other readers).
        from importer import _target_relpath
        with self.assertRaises(ImportFailure) as ctx:
            _target_relpath("a\x00b.txt", 0)
        self.assertEqual(ctx.exception.code, ERR_PATH_ESCAPE)

    def test_empty_target_rejected(self):
        self.assert_rejected(["./"], ERR_PATH_EMPTY)


class CollisionKeyTests(unittest.TestCase):
    """CASE-COLL-CASE-001 / CASE-COLL-UNI-001 at the key level."""

    def test_darwin_case_insensitive(self):
        self.assertEqual(collision_key("README.TXT", "darwin"),
                         collision_key("readme.txt", "darwin"))

    def test_darwin_unicode_normalization_insensitive(self):
        nfc = "résumé.txt"  # é as a single code point
        nfd = unicodedata.normalize("NFD", nfc)  # e + combining accent
        self.assertNotEqual(nfc.encode("utf-8"), nfd.encode("utf-8"))
        self.assertEqual(collision_key(nfc, "darwin"),
                         collision_key(nfd, "darwin"))

    def test_linux_case_sensitive(self):
        self.assertNotEqual(collision_key("README.TXT", "linux"),
                            collision_key("readme.txt", "linux"))

    def test_windows_case_insensitive_nfc(self):
        self.assertEqual(collision_key("Résumé.TXT", "windows"),
                         collision_key("résumé.txt", "windows"))

    def test_plan_detects_case_collision_on_darwin(self):
        with self.assertRaises(ImportFailure) as ctx:
            plan_names(["Readme.txt", "README.TXT"], platform="darwin")
        self.assertEqual(ctx.exception.code, ERR_NAME_COLLISION)

    def test_plan_allows_case_difference_on_linux(self):
        plans = plan_names(["Readme.txt", "README.TXT"], platform="linux")
        self.assertEqual(len(plans), 2)


if __name__ == "__main__":
    unittest.main()
