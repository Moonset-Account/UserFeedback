import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
BOARD = os.path.join(ROOT, "samples", "board.json")
DEMO = os.path.join(ROOT, "samples", "demo.bin")


def run_cli(args):
    return subprocess.run(
        [sys.executable, APP] + args,
        capture_output=True, text=True, timeout=30, cwd=ROOT,
    )


class CliTests(unittest.TestCase):
    def test_demo_sample_report(self):
        proc = run_cli([BOARD, DEMO])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["summary"]["images_total"], 1)
        image = report["images"][0]
        self.assertEqual(image["status"], "checked")
        self.assertEqual(image["header"]["record_count"], 5)
        self.assertEqual(image["header"]["header_size"], 88)

        # Exactly one true overlap: P1 (record @24) vs P2 (record @40).
        overlaps = [f for f in image["findings"] if f["type"] == "overlap"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["record_offsets"], [24, 40])
        self.assertEqual(overlaps[0]["overlap_range"], [700, 768])
        # The adjacent pair P0 (@8) / P1 (@24) must not be reported.
        for finding in overlaps:
            self.assertNotIn(8, finding["record_offsets"])

        out = [f for f in image["findings"] if f["type"] == "out_of_image"]
        self.assertEqual([f["record_offset"] for f in out], [56])

        reserved = [f for f in image["findings"]
                    if f["type"] == "reserved_region"]
        self.assertEqual([(f["record_offset"], f["region"]) for f in reserved],
                         [(72, "bootrom")])

    def test_bad_header_image_skipped_others_continue(self):
        tmpdir = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__)))
        try:
            bad = os.path.join(tmpdir, "bad.bin")
            with open(bad, "wb") as fh:
                fh.write(b"NOPE" + bytes(64))
            proc = run_cli([BOARD, bad, DEMO])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["images"][0]["status"], "skipped")
            self.assertIn("magic", report["images"][0]["skip_reason"])
            self.assertEqual(report["images"][1]["status"], "checked")
            self.assertEqual(report["summary"]["checked"], 1)
            self.assertEqual(report["summary"]["skipped"], 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_missing_image_file_is_usage_error(self):
        proc = run_cli([BOARD, os.path.join(ROOT, "samples",
                                            "does-not-exist.bin")])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error", proc.stderr.lower())


if __name__ == "__main__":
    unittest.main()
