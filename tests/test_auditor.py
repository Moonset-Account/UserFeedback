"""Unittest suite for the offline image-layer credential auditor.

All fixtures are synthetic (see sample_fixtures.py); no real credentials and
no network/system scanning are involved.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auditor
import sample_fixtures as fx

RULES = auditor.compile_rules([{"id": fx.RULE_ID, "pattern": fx.RULE_PATTERN}])


class WhiteoutDeletionTest(unittest.TestCase):
    """C01/C06: a later whiteout deletes the file from the final view, but the
    historical layer that contained the marker is still recorded."""

    def test_whiteout_deletion_history_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("app/config.env", b"token=" + fx.TOKEN)])
            l1 = fx.write_layer(td, "l1.tar", [("app/.wh.config.env", b"")])
            res = auditor.audit([l0, l1], RULES)

            self.assertTrue(res.view_complete)
            self.assertEqual(len(res.matches), 1)
            m = res.matches[0]
            self.assertEqual(m.rule_id, fx.RULE_ID)
            self.assertEqual(m.layer_index, 0)
            self.assertEqual(m.path, "app/config.env")
            self.assertIs(m.visible_final, False)
            self.assertNotIn("app/config.env", res.view.entries)

            report = auditor.report_to_dict(res)
            self.assertEqual(report["summary"]["layers_with_matches"], [0])
            self.assertEqual(report["summary"]["hidden"], 1)
            self.assertEqual(report["summary"]["visible"], 0)

    def test_whiteout_directory_removes_children(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [
                ("conf/", None),
                ("conf/secret/key.pem", fx.TOKEN),
                ("conf/other.txt", fx.BENIGN),
            ])
            l1 = fx.write_layer(td, "l1.tar", [(".wh.conf", b"")])
            res = auditor.audit([l0, l1], RULES)
            self.assertEqual(len(res.matches), 1)
            self.assertIs(res.matches[0].visible_final, False)
            self.assertEqual(res.view.entries, {})


class OverwriteTest(unittest.TestCase):
    """C02: same-path overwrite in a later layer."""

    def test_same_path_overwrite_hides_earlier_marker(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("secret.txt", fx.TOKEN)])
            l1 = fx.write_layer(td, "l1.tar", [("secret.txt", fx.BENIGN)])
            res = auditor.audit([l0, l1], RULES)
            self.assertEqual(len(res.matches), 1)
            self.assertEqual(res.matches[0].layer_index, 0)
            self.assertIs(res.matches[0].visible_final, False)
            self.assertEqual(res.view.entries.get("secret.txt"), 1)

    def test_same_path_overwrite_with_new_marker(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("secret.txt", fx.TOKEN)])
            l1 = fx.write_layer(td, "l1.tar", [("secret.txt", fx.TOKEN2)])
            res = auditor.audit([l0, l1], RULES)
            self.assertEqual(len(res.matches), 2)
            by_layer = {m.layer_index: m for m in res.matches}
            self.assertIs(by_layer[0].visible_final, False)
            self.assertIs(by_layer[1].visible_final, True)
            self.assertNotEqual(by_layer[0].fingerprint, by_layer[1].fingerprint)


class OpaqueMarkerOrderTest(unittest.TestCase):
    """C03: opaque dir marker hides only earlier layers; same-layer additions
    stay visible in BOTH archive orders (marker before/after the new file)."""

    def _build(self, td, order):
        l0 = fx.write_layer(td, "l0.tar", [
            ("etc/", None),
            ("etc/secret.key", fx.TOKEN),
            ("etc/keep.txt", fx.BENIGN),
        ])
        members = [("etc/.wh..wh..opq", b""), ("etc/new.txt", fx.BENIGN)]
        if order == "file_first":
            members = [members[1], members[0]]
        l1 = fx.write_layer(td, "l1.tar", members)
        return [l0, l1]

    def test_opaque_marker_two_archive_orders(self):
        views = []
        for order in ("marker_first", "file_first"):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as td:
                res = auditor.audit(self._build(td, order), RULES)
                self.assertTrue(res.view_complete)
                # Earlier-layer marker is hidden by the opaque marker.
                self.assertEqual(len(res.matches), 1)
                self.assertEqual(res.matches[0].layer_index, 0)
                self.assertEqual(res.matches[0].path, "etc/secret.key")
                self.assertIs(res.matches[0].visible_final, False)
                # Same-layer addition stays visible; earlier content is gone.
                self.assertIn("etc/new.txt", res.view.entries)
                self.assertEqual(res.view.entries["etc/new.txt"], 1)
                self.assertNotIn("etc/secret.key", res.view.entries)
                self.assertNotIn("etc/keep.txt", res.view.entries)
                views.append(set(res.view.entries))
        self.assertEqual(views[0], views[1])


class CleanScanTest(unittest.TestCase):
    """C04: layers without any marker produce an empty match list."""

    def test_no_credentials_clean_scan(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("a.txt", fx.BENIGN)])
            l1 = fx.write_layer(td, "l1.tar", [("b.txt", fx.BENIGN), ("a.txt", fx.BENIGN)])
            res = auditor.audit([l0, l1], RULES)
            report = auditor.report_to_dict(res)
            self.assertTrue(report["view_complete"])
            self.assertEqual(report["matches"], [])
            self.assertEqual(report["summary"]["total_matches"], 0)
            self.assertEqual(report["summary"]["layers_with_matches"], [])
            self.assertEqual(res.view.entries.get("a.txt"), 1)


class CorruptedLayerTest(unittest.TestCase):
    """C05: a corrupted later layer keeps proven historical matches and marks
    the affected final visibility as unknown."""

    def test_corrupted_later_layer_keeps_history_visibility_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("secret.txt", fx.TOKEN)])
            l1 = fx.write_garbage_layer(td, "l1.tar")
            res = auditor.audit([l0, l1], RULES)

            self.assertFalse(res.view_complete)
            self.assertEqual(res.corrupted_layer, 1)
            self.assertEqual(res.layers[0]["status"], "ok")
            self.assertEqual(res.layers[1]["status"], "corrupted")
            self.assertTrue(res.layers[1]["error"])

            # Historical match is preserved, final visibility is unknown.
            self.assertEqual(len(res.matches), 1)
            self.assertEqual(res.matches[0].layer_index, 0)
            self.assertEqual(res.matches[0].visible_final, "unknown")

            report = auditor.report_to_dict(res)
            self.assertEqual(report["summary"]["unknown"], 1)
            self.assertEqual(report["summary"]["layers_with_matches"], [0])

    def test_truncated_layer_also_corrupted(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("secret.txt", fx.TOKEN)])
            l1 = fx.write_truncated_layer(td, "l1.tar")
            res = auditor.audit([l0, l1], RULES)
            self.assertFalse(res.view_complete)
            self.assertEqual(res.layers[1]["status"], "corrupted")
            self.assertEqual(res.matches[0].visible_final, "unknown")

    def test_corrupted_after_proven_deletion_keeps_proven_hidden(self):
        """Deletion proven by intact layers stays 'not visible' even when a
        later layer is corrupted; only undecided visibility becomes unknown."""
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("secret.txt", fx.TOKEN)])
            l1 = fx.write_layer(td, "l1.tar", [(".wh.secret.txt", b"")])
            l2 = fx.write_garbage_layer(td, "l2.tar")
            res = auditor.audit([l0, l1, l2], RULES)
            self.assertFalse(res.view_complete)
            self.assertEqual(res.layers[1]["status"], "ok")
            self.assertEqual(res.layers[2]["status"], "corrupted")
            self.assertEqual(len(res.matches), 1)
            self.assertIs(res.matches[0].visible_final, False)

    def test_corrupted_first_layer_skips_rest(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_garbage_layer(td, "l0.tar")
            l1 = fx.write_layer(td, "l1.tar", [("secret.txt", fx.TOKEN)])
            res = auditor.audit([l0, l1], RULES)
            self.assertFalse(res.view_complete)
            self.assertEqual(res.layers[0]["status"], "corrupted")
            self.assertEqual(res.layers[1]["status"], "skipped")
            # The intact later layer is never applied to an uncertain view.
            self.assertEqual(res.matches, [])


class RedactionTest(unittest.TestCase):
    """C07: the report contains only rule/layer/path/fingerprint -- no raw
    marker content."""

    def test_report_contains_no_raw_secret(self):
        with tempfile.TemporaryDirectory() as td:
            layers = fx.build_sample_layers(td)
            res = auditor.audit(layers, RULES)
            text = json.dumps(auditor.report_to_dict(res))
            self.assertNotIn(fx.TOKEN.decode(), text)
            self.assertNotIn("SYNTHETIC-TOKEN", text)
            for m in res.matches:
                self.assertTrue(m.fingerprint.startswith("sha256:"))
                self.assertEqual(len(m.fingerprint), len("sha256:") + 16)


class CliEndToEndTest(unittest.TestCase):
    """C08: CLI entry point on the delivered sample fixtures."""

    def _run_cli(self, args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "app.py")] + args,
            capture_output=True, text=True, timeout=60, cwd=ROOT)

    def test_cli_sample_report(self):
        with tempfile.TemporaryDirectory() as td:
            layers = fx.build_sample_layers(td)
            rules = fx.write_rules(td)
            out = os.path.join(td, "report.json")
            proc = self._run_cli(["--layers"] + layers + ["--rules", rules,
                                                          "--output", out])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(out, encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertTrue(report["view_complete"])
            self.assertEqual(report["summary"]["total_matches"], 2)
            self.assertEqual(report["summary"]["visible"], 0)
            self.assertEqual(report["summary"]["hidden"], 2)
            self.assertEqual(report["summary"]["layers_with_matches"], [0])
            paths = {(m["layer"], m["path"]) for m in report["matches"]}
            self.assertEqual(paths, {(0, "app/config.env"), (0, "etc/secret.key")})

    def test_cli_fail_on_visible(self):
        with tempfile.TemporaryDirectory() as td:
            l0 = fx.write_layer(td, "l0.tar", [("secret.txt", fx.TOKEN)])
            rules = fx.write_rules(td)
            proc = self._run_cli(["--layers", l0, "--rules", rules,
                                  "--fail-on", "visible"])
            self.assertEqual(proc.returncode, 1, proc.stderr)
            report = json.loads(proc.stdout)
            self.assertEqual(report["summary"]["visible"], 1)

    def test_cli_missing_layer_is_usage_error(self):
        with tempfile.TemporaryDirectory() as td:
            rules = fx.write_rules(td)
            proc = self._run_cli(["--layers", os.path.join(td, "nope.tar"),
                                  "--rules", rules])
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
