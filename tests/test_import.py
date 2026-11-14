"""End-to-end import tests with synthetic archives in temp directories.

Covers: CASE-BATCH-001/002, CASE-ATOMIC-CORRUPT-001, CASE-ATOMIC-COLL-001,
CASE-ATOMIC-LIMIT-001, CASE-LIMIT-ENTRY-001, CASE-LIMIT-TOTAL-001,
CASE-PRECHECK-001/002, CASE-LINK-001, CASE-SPECIAL-001, CASE-REPORT-001.
"""

import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import helpers  # noqa: E402
from importer import (  # noqa: E402
    ERR_BAD_ARCHIVE,
    ERR_BATCH_EXISTS,
    ERR_ENCRYPTED,
    ERR_ENTRY_TOO_LARGE,
    ERR_FILE_DIR_CONFLICT,
    ERR_NAME_COLLISION,
    ERR_NOT_A_DIR,
    ERR_PATH_ESCAPE,
    ERR_SPECIAL_FILE,
    ERR_SYMLINK,
    ERR_TOTAL_TOO_LARGE,
    ImportFailure,
    import_archive,
)


class ImportTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zipimport-test-")
        self.inbox = os.path.join(self.tmp, "inbox")    # untrusted archives
        self.shared = os.path.join(self.tmp, "shared")  # shared material area
        os.makedirs(self.inbox)
        os.makedirs(self.shared)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_zip(self, name, entries, **kw):
        path = os.path.join(self.inbox, name)
        return helpers.write_zip(path, entries, **kw)

    def assert_shared_clean(self, before):
        """Shared area unchanged and no staging leftovers."""
        self.assertEqual(helpers.snapshot(self.shared), before)
        self.assertEqual(
            [n for n in os.listdir(self.shared) if n.startswith(".staging-")],
            [])


class HappyPathTests(ImportTestBase):
    """CASE-BATCH-001: the result of a batch import is one new directory."""

    def test_happy_path_creates_new_batch_dir(self):
        zpath = self.make_zip("good.zip", [
            ("docs/a.txt", b"alpha", helpers.MODE_FILE),
            ("b.txt", b"beta", helpers.MODE_FILE),
            ("empty/", b"", helpers.MODE_DIR),
        ])
        report = import_archive(zpath, self.shared, batch_name="batch-1")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["batch_name"], "batch-1")
        batch = os.path.join(self.shared, "batch-1")
        self.assertTrue(os.path.isdir(batch))
        self.assertEqual(helpers.read_file(batch, "docs/a.txt"), b"alpha")
        self.assertEqual(helpers.read_file(batch, "b.txt"), b"beta")
        self.assertTrue(os.path.isdir(os.path.join(batch, "empty")))
        self.assertEqual(report["total_bytes"], 9)
        self.assertEqual(
            sorted(e["path"] for e in report["entries"]),
            ["b.txt", "docs/a.txt"])

    def test_default_batch_name_is_unique_per_content(self):
        zpath = self.make_zip("good.zip", [("a.txt", b"x", helpers.MODE_FILE)])
        report = import_archive(zpath, self.shared)
        self.assertTrue(report["batch_name"].startswith("good-"))
        self.assertTrue(os.path.isdir(report["batch_dir"]))

    def test_existing_shared_content_untouched(self):
        os.makedirs(os.path.join(self.shared, "old-batch"))
        with open(os.path.join(self.shared, "old-batch", "keep.txt"), "wb") as f:
            f.write(b"keep")
        before = helpers.snapshot(self.shared)
        zpath = self.make_zip("good.zip", [("a.txt", b"x", helpers.MODE_FILE)])
        import_archive(zpath, self.shared, batch_name="new-batch")
        after = helpers.snapshot(self.shared)
        self.assertEqual(helpers.read_file(self.shared, "old-batch/keep.txt"),
                         b"keep")
        for item in before:  # every pre-existing item is still there
            self.assertIn(item, after)
        self.assertIn("new-batch/", after)
        self.assertIn("new-batch/a.txt", after)
        self.assertEqual(len(after), len(before) + 2)

    def test_empty_zip_imports_as_empty_batch_dir(self):
        zpath = self.make_zip("empty.zip", [])
        report = import_archive(zpath, self.shared, batch_name="empty-batch")
        self.assertEqual(report["total_bytes"], 0)
        self.assertTrue(os.path.isdir(os.path.join(self.shared, "empty-batch")))

    def test_shared_dir_must_exist(self):
        zpath = self.make_zip("good.zip", [("a.txt", b"x", helpers.MODE_FILE)])
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, os.path.join(self.tmp, "missing"))
        self.assertEqual(ctx.exception.code, ERR_NOT_A_DIR)


class AtomicityTests(ImportTestBase):
    """CASE-ATOMIC-*: on failure the shared area shows no partial material."""

    def test_truncated_archive_rejected_no_partial(self):
        zpath = self.make_zip("broken.zip", [("a.txt", b"x" * 100,
                                              helpers.MODE_FILE)])
        with open(zpath, "r+b") as f:
            f.truncate(os.path.getsize(zpath) // 2)
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_BAD_ARCHIVE)
        self.assertIsNone(ctx.exception.entry)
        self.assert_shared_clean(before)

    def test_bad_crc_during_extraction_no_partial(self):
        zpath = self.make_zip("badcrc.zip", [
            ("first.txt", b"ok-content", helpers.MODE_FILE, helpers.STORED),
            ("second.txt", b"corrupt-me" * 10, helpers.MODE_FILE,
             helpers.STORED),
        ])
        helpers.flip_data_byte(zpath, "second.txt")
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_BAD_ARCHIVE)
        self.assertEqual(ctx.exception.entry, "second.txt")
        self.assertEqual(ctx.exception.entry_index, 1)
        self.assert_shared_clean(before)

    def test_name_collision_no_partial(self):
        zpath = self.make_zip("clash.zip", [
            ("Readme.txt", b"one", helpers.MODE_FILE),
            ("README.TXT", b"two", helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared, platform="darwin")
        self.assertEqual(ctx.exception.code, ERR_NAME_COLLISION)
        self.assert_shared_clean(before)

    def test_zip_slip_no_partial_and_no_escape(self):
        zpath = self.make_zip("slip.zip", [
            ("ok.txt", b"fine", helpers.MODE_FILE),
            ("../evil.txt", b"pwn", helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_PATH_ESCAPE)
        self.assertEqual(ctx.exception.entry, "../evil.txt")
        self.assert_shared_clean(before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_over_limit_no_partial(self):
        zpath = self.make_zip("big.zip", [
            ("a.bin", b"a" * 60, helpers.MODE_FILE),
            ("b.bin", b"b" * 60, helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared,
                           max_entry_bytes=100, max_total_bytes=100)
        self.assertEqual(ctx.exception.code, ERR_TOTAL_TOO_LARGE)
        self.assert_shared_clean(before)


class EntryTypeTests(ImportTestBase):
    """CASE-LINK-001 / CASE-SPECIAL-001: links and special files rejected."""

    def test_symlink_rejected(self):
        zpath = self.make_zip("link.zip", [
            ("real.txt", b"data", helpers.MODE_FILE),
            ("link.txt", b"/etc/passwd", helpers.MODE_SYMLINK),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_SYMLINK)
        self.assertEqual(ctx.exception.entry, "link.txt")
        self.assert_shared_clean(before)

    def test_special_file_rejected(self):
        zpath = self.make_zip("fifo.zip", [
            ("pipe", b"", helpers.MODE_FIFO),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_SPECIAL_FILE)
        self.assertEqual(ctx.exception.entry, "pipe")
        self.assert_shared_clean(before)

    def test_encrypted_entry_rejected(self):
        zpath = self.make_zip("secret.zip", [
            ("secret.txt", b"data", helpers.MODE_FILE),
        ])
        helpers.set_encryption_flag(zpath)
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_ENCRYPTED)
        self.assertEqual(ctx.exception.entry, "secret.txt")
        self.assert_shared_clean(before)

    def test_file_dir_conflict_rejected(self):
        zpath = self.make_zip("conflict.zip", [
            ("a", b"file", helpers.MODE_FILE),
            ("a/b.txt", b"nested", helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_FILE_DIR_CONFLICT)
        self.assert_shared_clean(before)

    def test_unicode_collision_rejected(self):
        import unicodedata
        nfc = "café.txt"  # é as a single code point
        nfd = unicodedata.normalize("NFD", nfc)  # é as e + combining accent
        self.assertNotEqual(nfc.encode("utf-8"), nfd.encode("utf-8"))
        zpath = self.make_zip("uni.zip", [
            (nfc, b"nfc", helpers.MODE_FILE),
            (nfd, b"nfd", helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared, platform="darwin")
        self.assertEqual(ctx.exception.code, ERR_NAME_COLLISION)
        self.assert_shared_clean(before)


class SizeLimitTests(ImportTestBase):
    """CASE-LIMIT-*: actual expanded bytes are limited per entry and in
    total; CASE-PRECHECK-*: declared sizes are pre-check only."""

    def test_declared_size_precheck_entry(self):
        """CASE-PRECHECK-001: declared size over the limit fails in the
        pre-check, before anything is extracted."""
        zpath = self.make_zip("declared.zip", [
            ("big.bin", b"z" * 1000, helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared, max_entry_bytes=500)
        self.assertEqual(ctx.exception.code, ERR_ENTRY_TOO_LARGE)
        self.assertIn("declared", ctx.exception.message)
        self.assert_shared_clean(before)

    def test_declared_size_precheck_total(self):
        zpath = self.make_zip("declared-total.zip", [
            ("a.bin", b"a" * 400, helpers.MODE_FILE),
            ("b.bin", b"b" * 400, helpers.MODE_FILE),
        ])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared,
                           max_entry_bytes=1000, max_total_bytes=500)
        self.assertEqual(ctx.exception.code, ERR_TOTAL_TOO_LARGE)
        self.assertIn("declared", ctx.exception.message)
        self.assert_shared_clean(before)

    def test_lying_declared_size_rejected(self):
        """CASE-PRECHECK-002: declared sizes are not trusted. This archive
        declares 1 byte but stores 500; the import must be rejected (the
        CPython reader caps output at the declared size, so the lie surfaces
        as a CRC failure) and the shared area must stay clean."""
        payload = b"A" * 500
        zpath = self.make_zip("lying.zip", [
            ("lying.bin", payload, helpers.MODE_FILE, helpers.STORED),
        ])
        helpers.patch_central_file_size(zpath, "lying.bin", 1)  # declared = 1
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared, max_entry_bytes=100)
        self.assertIn(ctx.exception.code, (ERR_BAD_ARCHIVE, ERR_ENTRY_TOO_LARGE))
        self.assertEqual(ctx.exception.entry, "lying.bin")
        self.assert_shared_clean(before)

    def test_entry_limit_enforced_on_actual_streamed_bytes(self):
        """CASE-LIMIT-ENTRY-001: the streaming check counts actual expanded
        bytes, independent of declared metadata (white-box: a stub reader
        yields more than the plan's declared size)."""
        import io
        from importer import EntryPlan, _extract_all

        class StubZip:
            def infolist(self):
                return [object()]

            def open(self, info, mode="r"):
                return io.BytesIO(b"A" * 500)  # actual: 500 bytes

        plans = [EntryPlan(0, "x.bin", "x.bin", False, 1)]  # declared: 1
        staging = tempfile.mkdtemp(dir=self.tmp)
        try:
            with self.assertRaises(ImportFailure) as ctx:
                _extract_all(StubZip(), plans, staging,
                             max_entry_bytes=100, max_total_bytes=10 ** 6)
            self.assertEqual(ctx.exception.code, ERR_ENTRY_TOO_LARGE)
            self.assertNotIn("declared", ctx.exception.message)
            self.assertEqual(ctx.exception.entry, "x.bin")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)

    def test_total_limit_enforced_on_actual_streamed_bytes(self):
        """CASE-LIMIT-TOTAL-001: each entry is under the per-entry limit but
        the actual expanded total exceeds the package limit (white-box)."""
        import io
        from importer import EntryPlan, _extract_all

        class StubZip:
            def infolist(self):
                return [object(), object(), object()]

            def open(self, info, mode="r"):
                return io.BytesIO(b"a" * 40)

        plans = [EntryPlan(i, "f%d.bin" % i, "f%d.bin" % i, False, 1)
                 for i in range(3)]
        staging = tempfile.mkdtemp(dir=self.tmp)
        try:
            with self.assertRaises(ImportFailure) as ctx:
                _extract_all(StubZip(), plans, staging,
                             max_entry_bytes=100, max_total_bytes=100)
            self.assertEqual(ctx.exception.code, ERR_TOTAL_TOO_LARGE)
            self.assertNotIn("declared", ctx.exception.message)
            self.assertEqual(ctx.exception.entry, "f2.bin")
        finally:
            import shutil
            shutil.rmtree(staging, ignore_errors=True)

    def test_exactly_at_limit_succeeds(self):
        zpath = self.make_zip("exact.zip", [
            ("a.bin", b"a" * 100, helpers.MODE_FILE),
        ])
        report = import_archive(zpath, self.shared,
                                max_entry_bytes=100, max_total_bytes=100,
                                batch_name="exact")
        self.assertEqual(report["total_bytes"], 100)


class BatchIdentityTests(ImportTestBase):
    """CASE-BATCH-002: an existing batch directory is never overwritten."""

    def test_batch_exists_not_overwritten(self):
        zpath = self.make_zip("one.zip", [("a.txt", b"first", helpers.MODE_FILE)])
        import_archive(zpath, self.shared, batch_name="batch-x")
        zpath2 = self.make_zip("two.zip", [("b.txt", b"second",
                                            helpers.MODE_FILE)])
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath2, self.shared, batch_name="batch-x")
        self.assertEqual(ctx.exception.code, ERR_BATCH_EXISTS)
        self.assert_shared_clean(before)
        self.assertEqual(helpers.read_file(self.shared, "batch-x/a.txt"),
                         b"first")

    def test_reimport_same_archive_fails_instead_of_duplicating(self):
        zpath = self.make_zip("same.zip", [("a.txt", b"x", helpers.MODE_FILE)])
        import_archive(zpath, self.shared)
        before = helpers.snapshot(self.shared)
        with self.assertRaises(ImportFailure) as ctx:
            import_archive(zpath, self.shared)
        self.assertEqual(ctx.exception.code, ERR_BATCH_EXISTS)
        self.assert_shared_clean(before)


class FailureReportTests(ImportTestBase):
    """CASE-REPORT-001: failure reports locate the archive entry."""

    def test_report_locates_entry_name_and_index(self):
        zpath = self.make_zip("report.zip", [
            ("good1.txt", b"a", helpers.MODE_FILE),
            ("good2.txt", b"b", helpers.MODE_FILE),
            ("../evil.txt", b"c", helpers.MODE_FILE),
        ])
        try:
            import_archive(zpath, self.shared)
            self.fail("expected ImportFailure")
        except ImportFailure as failure:
            report = failure.to_report()
        self.assertEqual(report["status"], "error")
        err = report["error"]
        self.assertEqual(err["code"], ERR_PATH_ESCAPE)
        self.assertEqual(err["entry"], "../evil.txt")
        self.assertEqual(err["entry_index"], 2)
        self.assertIn("..", err["message"])

    def test_non_zip_archive_report_has_no_entry(self):
        zpath = os.path.join(self.inbox, "not-a-zip.bin")
        with open(zpath, "wb") as f:
            f.write(b"\x00" * 64)
        try:
            import_archive(zpath, self.shared)
            self.fail("expected ImportFailure")
        except ImportFailure as failure:
            report = failure.to_report()
        self.assertEqual(report["error"]["code"], ERR_BAD_ARCHIVE)
        self.assertIsNone(report["error"]["entry"])
        self.assertIsNone(report["error"]["entry_index"])


if __name__ == "__main__":
    unittest.main()
