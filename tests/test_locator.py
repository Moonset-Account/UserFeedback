"""Unit tests for the cross-build diagnostic symbol locator."""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from locator import (  # noqa: E402
    ModuleRange,
    REASON_BUILD_MISMATCH,
    REASON_MODULE_OVERLAP,
    REASON_NO_APP_FRAME,
    REASON_NO_SYMBOL,
    REASON_OUTSIDE_MODULES,
    REASON_SYMBOL_OVERLAP,
    SymbolRange,
    build_report,
    parse_address,
    process_record,
    resolve_frame,
)

SAMPLE_DEMO = os.path.join(ROOT, "samples", "demo.json")
APP_PY = os.path.join(ROOT, "app.py")


def load_demo_payload():
    with open(SAMPLE_DEMO, "r", encoding="utf-8") as fh:
        return json.load(fh)


class AddressParsingTest(unittest.TestCase):
    def test_hex_and_decimal_strings(self):
        self.assertEqual(parse_address("0x1000"), 0x1000)
        self.assertEqual(parse_address("4096"), 4096)
        self.assertEqual(parse_address(4096), 4096)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            parse_address(True)
        with self.assertRaises(ValueError):
            parse_address(None)


class HalfOpenRangeTest(unittest.TestCase):
    """Right endpoints are excluded for both module and symbol ranges."""

    def test_module_range_right_endpoint_excluded(self):
        mod = ModuleRange("app", 0x1000, 0x2000)
        self.assertTrue(mod.contains(0x1000))
        self.assertTrue(mod.contains(0x1FFF))
        self.assertFalse(mod.contains(0x2000))

    def test_symbol_range_right_endpoint_excluded(self):
        sym = SymbolRange(0x100, 0x200, "f", "f.c:1", True)
        self.assertTrue(sym.contains(0x100))
        self.assertTrue(sym.contains(0x1FF))
        self.assertFalse(sym.contains(0x200))

    def test_address_at_module_end_is_unresolved(self):
        modules = [ModuleRange("app", 0x1000, 0x2000)]
        symbols = {"app": [SymbolRange(0x0, 0x1000, "f", "f.c:1", True)]}
        frame = resolve_frame(0x2000, modules, symbols)
        self.assertFalse(frame["resolved"])
        self.assertEqual(frame["reason"], REASON_OUTSIDE_MODULES)
        self.assertEqual(frame["address"], "0x2000")

    def test_address_at_symbol_end_falls_through(self):
        modules = [ModuleRange("app", 0x1000, 0x2000)]
        symbols = {"app": [SymbolRange(0x0, 0x100, "f", "f.c:1", True)]}
        # 0x1100 is inside the module but exactly at the symbol's rel_end.
        frame = resolve_frame(0x1100, modules, symbols)
        self.assertFalse(frame["resolved"])
        self.assertEqual(frame["reason"], REASON_NO_SYMBOL)
        self.assertEqual(frame["relative_address"], "0x100")


class FrameResolutionTest(unittest.TestCase):
    def test_missing_symbol_keeps_raw_frame(self):
        modules = [ModuleRange("app", 0x1000, 0x2000)]
        symbols = {"app": [SymbolRange(0x0, 0x100, "f", "f.c:1", True)]}
        frame = resolve_frame(0x1800, modules, symbols)
        self.assertFalse(frame["resolved"])
        self.assertEqual(frame["reason"], REASON_NO_SYMBOL)
        self.assertEqual(frame["address"], "0x1800")
        self.assertEqual(frame["module"], "app")

    def test_overlapping_module_ranges_are_ambiguous(self):
        modules = [
            ModuleRange("app", 0x1000, 0x2000),
            ModuleRange("plugin", 0x1800, 0x2800),
        ]
        frame = resolve_frame(0x1900, modules, {"app": [], "plugin": []})
        self.assertFalse(frame["resolved"])
        self.assertEqual(frame["reason"], REASON_MODULE_OVERLAP)
        self.assertIn("app", frame["detail"])
        self.assertIn("plugin", frame["detail"])

    def test_overlapping_symbol_ranges_are_ambiguous(self):
        modules = [ModuleRange("app", 0x1000, 0x2000)]
        symbols = {
            "app": [
                SymbolRange(0x0, 0x200, "f", "f.c:1", True),
                SymbolRange(0x100, 0x300, "g", "g.c:1", True),
            ]
        }
        frame = resolve_frame(0x1100, modules, symbols)
        self.assertFalse(frame["resolved"])
        self.assertEqual(frame["reason"], REASON_SYMBOL_OVERLAP)


class GroupingTest(unittest.TestCase):
    def test_same_code_different_load_addresses_grouped(self):
        """diag-1 and diag-2 crash at the same relative offset in module
        'app' but the module is loaded at different bases in the two
        builds; they must land in one group."""
        report = build_report(load_demo_payload())
        self.assertEqual(len(report["groups"]), 1)
        group = report["groups"][0]
        self.assertEqual(group["category"], "SEGV")
        self.assertEqual(group["function"], "parse_config")
        self.assertEqual(group["source"], "src/config.c:42")
        member_ids = sorted(m["diagnostic_id"] for m in group["members"])
        self.assertEqual(member_ids, ["diag-1", "diag-2"])
        # The absolute addresses differ, the relative address matches.
        addrs = {m["diagnostic_id"]: m["key_frame_address"] for m in group["members"]}
        self.assertNotEqual(addrs["diag-1"], addrs["diag-2"])
        rels = {m["relative_address"] for m in group["members"]}
        self.assertEqual(rels, {"0x140"})

    def test_first_app_frame_skips_system_frames(self):
        """The top frame is libsys!sys_read (not app code); grouping must
        use the first confirmable *application* frame below it."""
        report = build_report(load_demo_payload())
        group = report["groups"][0]
        self.assertEqual(group["function"], "parse_config")

    def test_build_mismatch_listed_separately(self):
        report = build_report(load_demo_payload())
        unresolved = {u["diagnostic_id"]: u for u in report["unresolved"]}
        self.assertIn("diag-3", unresolved)
        self.assertEqual(unresolved["diag-3"]["reason"], REASON_BUILD_MISMATCH)
        # Raw frames are preserved for the mismatched record.
        frames = unresolved["diag-3"]["frames"]
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0]["resolved"])
        self.assertEqual(frames[0]["reason"], REASON_BUILD_MISMATCH)
        self.assertEqual(frames[0]["address"], "0x300000140")

    def test_missing_symbol_record_unresolved_with_raw_frame(self):
        report = build_report(load_demo_payload())
        unresolved = {u["diagnostic_id"]: u for u in report["unresolved"]}
        self.assertIn("diag-4", unresolved)
        self.assertEqual(unresolved["diag-4"]["reason"], REASON_NO_APP_FRAME)
        frame = unresolved["diag-4"]["frames"][0]
        self.assertFalse(frame["resolved"])
        self.assertEqual(frame["reason"], REASON_NO_SYMBOL)
        self.assertEqual(frame["address"], "0x100000900")

    def test_category_separates_groups(self):
        payload = load_demo_payload()
        # Clone diag-1 with a different error category: same code, but
        # grouping keys on (category, function, source).
        extra = dict(payload["diagnostics"][0])
        extra["id"] = "diag-5"
        extra["category"] = "ABORT"
        payload["diagnostics"].append(extra)
        report = build_report(payload)
        self.assertEqual(len(report["groups"]), 2)
        categories = sorted(g["category"] for g in report["groups"])
        self.assertEqual(categories, ["ABORT", "SEGV"])

    def test_record_without_any_app_frame_unresolved(self):
        tables = {
            "b1": {"libsys": [SymbolRange(0x0, 0x80, "sys_read", "read.c:5", False)]}
        }
        record = {
            "id": "diag-x",
            "build": "b1",
            "category": "SEGV",
            "modules": [{"name": "libsys", "base": "0x7000", "end": "0x7100"}],
            "stack": ["0x7010"],
        }
        result = process_record(record, tables)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason"], REASON_NO_APP_FRAME)
        # The frame itself resolved fine; only the record is ungroupable.
        self.assertTrue(result["frames"][0]["resolved"])


class CliEndToEndTest(unittest.TestCase):
    def test_app_py_json_report_on_demo_sample(self):
        proc = subprocess.run(
            [sys.executable, APP_PY, SAMPLE_DEMO, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(len(report["groups"]), 1)
        member_ids = sorted(
            m["diagnostic_id"] for m in report["groups"][0]["members"]
        )
        self.assertEqual(member_ids, ["diag-1", "diag-2"])
        reasons = {u["diagnostic_id"]: u["reason"] for u in report["unresolved"]}
        self.assertEqual(reasons.get("diag-3"), REASON_BUILD_MISMATCH)
        self.assertEqual(reasons.get("diag-4"), REASON_NO_APP_FRAME)


if __name__ == "__main__":
    unittest.main()
