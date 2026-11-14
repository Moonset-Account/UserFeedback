import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import scanner
from scanner import (
    STATUS_CORRUPT,
    STATUS_SCANNED,
    STATUS_SKIPPED,
    STATUS_TRUNCATED,
    STATUS_UNREADABLE,
    build_report,
    load_markers,
    scan_directory,
    scan_file,
)

MARKER = b"MARKER-XYZ-123"  # 14 bytes, fictional


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="marker-scan-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def write_markers(self, text="MARKER-XYZ-123\n"):
        return self.write("markers.txt", text.encode("utf-8"))


class ChunkBoundaryTests(TempDirCase):
    def test_marker_straddling_chunk_boundary_found_once_on_right_line(self):
        # marker starts at offset 10, so with 16-byte chunks it straddles
        # the first read-chunk boundary; it sits on line 2.
        content = b"ab\n" + b"p" * 7 + MARKER + b"\ntail\n"
        self.assertEqual(content.index(MARKER), 10)
        path = self.write("a.log", content)
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_SCANNED)
        self.assertEqual(report.matches, [{"line": 2, "marker": 1}])

    def test_marker_larger_than_chunk_size(self):
        content = b"ab\n" + b"p" * 7 + MARKER + b"\ntail\n"
        path = self.write("a.log", content)
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=4, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_SCANNED)
        self.assertEqual(report.matches, [{"line": 2, "marker": 1}])

    def test_marker_at_every_line_counted_with_line_numbers(self):
        content = MARKER + b"\nmiddle\n" + MARKER + b"\n"
        path = self.write("a.log", content)
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=8, max_bytes=10**6)
        self.assertEqual(report.matches,
                         [{"line": 1, "marker": 1}, {"line": 3, "marker": 1}])

    def test_overlapping_occurrences_all_reported(self):
        path = self.write("a.log", b"aaaaaa\n")
        markers = load_markers(self.write_markers("aaaa\n"))
        report = scan_file(path, markers, chunk_size=4, max_bytes=10**6)
        self.assertEqual(len(report.matches), 3)
        self.assertTrue(all(m["line"] == 1 for m in report.matches))


class GzipTests(TempDirCase):
    def test_gzip_log_hit_with_line_number(self):
        body = b"start\n" + b"init done\n" + b"y" * 34 + MARKER + b"\nend\n"
        path = self.write("server.log.gz", gzip.compress(body))
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_SCANNED)
        self.assertTrue(report.compressed)
        self.assertEqual(report.matches, [{"line": 3, "marker": 1}])

    def test_corrupt_gzip_reported_as_corrupt(self):
        path = self.write("broken.gz", b"\x1f\x8b" + b"not a gzip stream")
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_CORRUPT)
        self.assertEqual(report.matches, [])


class CoverageStatusTests(TempDirCase):
    def test_unreadable_file_via_directory_path(self):
        markers = load_markers(self.write_markers())
        report = scan_file(self.tmp, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_UNREADABLE)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root can read chmod-000 files")
    def test_unreadable_file_via_permissions(self):
        path = self.write("secret.log", MARKER)
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_UNREADABLE)

    def test_broken_symlink_is_unreadable(self):
        link = os.path.join(self.tmp, "dead_link")
        os.symlink("nonexistent-target", link)
        markers = load_markers(self.write_markers())
        report = scan_file(link, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_UNREADABLE)

    def test_binary_file_is_skipped(self):
        path = self.write("payload.bin", b"\x00\x01" + MARKER)
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.status, STATUS_SKIPPED)
        self.assertEqual(report.matches, [])

    def test_size_limit_truncates_file(self):
        path = self.write("big.log", b"z" * 50)
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10)
        self.assertEqual(report.status, STATUS_TRUNCATED)
        self.assertEqual(report.bytes_scanned, 10)

    def test_marker_beyond_size_limit_not_reported(self):
        path = self.write("big.log", b"z" * 50 + MARKER + b"\n")
        markers = load_markers(self.write_markers())
        report = scan_file(path, markers, chunk_size=16, max_bytes=10)
        self.assertEqual(report.status, STATUS_TRUNCATED)
        self.assertEqual(report.matches, [])


class ReportTests(TempDirCase):
    def test_marker_ids_are_line_numbers_in_markers_file(self):
        markers_path = self.write_markers("\nMARKER-XYZ-123\n")
        markers = load_markers(markers_path)
        self.assertEqual([m.marker_id for m in markers], [2])
        path = self.write("a.log", MARKER + b"\n")
        report = scan_file(path, markers, chunk_size=16, max_bytes=10**6)
        self.assertEqual(report.matches, [{"line": 1, "marker": 2}])

    def test_report_never_echoes_marker_values(self):
        secret = "MARKER-XYZ-123"
        self.write("a.log", MARKER + b"\n")
        markers = load_markers(self.write_markers())
        reports = scan_directory(self.tmp, markers, chunk_size=16,
                                 max_bytes=10**6)
        report = build_report(self.tmp, markers, reports)
        text = json.dumps(report)
        self.assertNotIn(secret, text)
        for f in report["files"]:
            for m in f["matches"]:
                self.assertEqual(set(m.keys()), {"line", "marker"})

    def test_scan_directory_covers_all_statuses(self):
        self.write("clean.txt", b"all clear\n")
        self.write("broken.gz", b"\x1f\x8bgarbage")
        self.write("payload.bin", b"\x00binary")
        os.symlink("nonexistent-target", os.path.join(self.tmp, "dead_link"))
        markers = load_markers(self.write_markers())
        reports = scan_directory(self.tmp, markers, chunk_size=16,
                                 max_bytes=10**6)
        by_path = {r.path: r for r in reports}
        self.assertEqual(by_path["clean.txt"].status, STATUS_SCANNED)
        self.assertEqual(by_path["broken.gz"].status, STATUS_CORRUPT)
        self.assertEqual(by_path["payload.bin"].status, STATUS_SKIPPED)
        self.assertEqual(by_path["dead_link"].status, STATUS_UNREADABLE)
        # markers.txt itself contains the marker text and must be found
        self.assertEqual(by_path["markers.txt"].status, STATUS_SCANNED)
        self.assertEqual(by_path["markers.txt"].matches,
                         [{"line": 1, "marker": 1}])

    def test_load_markers_missing_file_raises(self):
        with self.assertRaises(OSError):
            load_markers(os.path.join(self.tmp, "nope.txt"))


class CliTests(TempDirCase):
    def run_cli(self, build_dir, markers_file, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py"),
             build_dir, markers_file, "--chunk-size", "16"] + list(extra),
            capture_output=True, text=True)

    def test_exit_0_when_clean(self):
        self.write("clean.txt", b"all clear\n")
        markers_file = self.write_markers()
        build = os.path.join(self.tmp, "build")
        os.makedirs(build)
        shutil.move(os.path.join(self.tmp, "clean.txt"),
                    os.path.join(build, "clean.txt"))
        proc = self.run_cli(build, markers_file)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["result"], "PASS")
        self.assertEqual(report["summary"]["matches_total"], 0)

    def test_exit_1_when_marker_found(self):
        build = os.path.join(self.tmp, "build")
        os.makedirs(build)
        with open(os.path.join(build, "a.log"), "wb") as fh:
            fh.write(b"log\n" + MARKER + b"\n")
        markers_file = self.write_markers()
        proc = self.run_cli(build, markers_file)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["result"], "FAIL")
        self.assertEqual(report["summary"]["matches_total"], 1)

    def test_exit_2_when_only_corrupt_files(self):
        build = os.path.join(self.tmp, "build")
        os.makedirs(build)
        with open(os.path.join(build, "broken.gz"), "wb") as fh:
            fh.write(b"\x1f\x8bgarbage")
        markers_file = self.write_markers()
        proc = self.run_cli(build, markers_file)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["files_corrupt"], 1)

    def test_exit_2_on_missing_markers_file(self):
        proc = self.run_cli(self.tmp, os.path.join(self.tmp, "nope.txt"))
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
