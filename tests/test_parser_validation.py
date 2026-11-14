"""解析器输入校验测试。"""
from __future__ import annotations

import unittest

from vuln_ledger.models import InputError
from vuln_ledger.parser import parse_input


def _base() -> dict:
    return {
        "commits": [
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []},
            {"id": "f", "parents": ["r"], "fix_ids": ["F1"], "reverts": []},
        ],
        "maintenance_lines": [
            {"name": "LTS", "roots": ["r"], "recognized_fix_ids": ["F1"]}
        ],
        "builds": [{"build_id": "B1", "line": "LTS", "source_commit": "f"}],
        "merge_resolutions": [],
    }


class ParserValidationTest(unittest.TestCase):
    def test_duplicate_commit_rejected(self) -> None:
        raw = _base()
        raw["commits"].append(
            {"id": "r", "parents": [], "fix_ids": [], "reverts": []}
        )
        with self.assertRaises(InputError):
            parse_input(raw)

    def test_duplicate_build_rejected(self) -> None:
        raw = _base()
        raw["builds"].append(
            {"build_id": "B1", "line": "LTS", "source_commit": "r"}
        )
        with self.assertRaises(InputError):
            parse_input(raw)

    def test_unknown_line_rejected(self) -> None:
        raw = _base()
        raw["builds"][0]["line"] = "NOPE"
        with self.assertRaises(InputError):
            parse_input(raw)

    def test_unknown_source_commit_rejected(self) -> None:
        raw = _base()
        raw["builds"][0]["source_commit"] = "ghost"
        with self.assertRaises(InputError):
            parse_input(raw)

    def test_bad_resolution_value_rejected(self) -> None:
        raw = _base()
        raw["merge_resolutions"] = [
            {"commit": "f", "fix_id": "F1", "resolved": "maybe"}
        ]
        with self.assertRaises(InputError):
            parse_input(raw)

    def test_missing_required_field_rejected(self) -> None:
        raw = _base()
        del raw["commits"][1]["id"]
        with self.assertRaises(InputError):
            parse_input(raw)

    def test_revert_object_null_fix_id_accepted(self) -> None:
        raw = _base()
        raw["commits"][1]["reverts"] = [{"fix_id": None}]
        ledger = parse_input(raw)
        self.assertEqual(ledger.commits["f"].revert_refs, [None])

    def test_non_object_input_rejected(self) -> None:
        with self.assertRaises(InputError):
            parse_input([1, 2, 3])

    def test_duplicate_recognized_fix_rejected(self) -> None:
        raw = _base()
        raw["maintenance_lines"][0]["recognized_fix_ids"] = ["F1", "F1"]
        with self.assertRaises(InputError):
            parse_input(raw)


if __name__ == "__main__":
    unittest.main()
