from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from info_flow_checker.cli import check_document, main


ROOT = Path(__file__).resolve().parents[1]


class CliAndDocumentTests(unittest.TestCase):
    def test_shipped_cancellation_example_is_secure(self) -> None:
        path = ROOT / "examples" / "cancellation.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        report = check_document(document)
        self.assertTrue(report.secure, report.describe())

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main([str(path)])
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["secure"])

        optimized_output = io.StringIO()
        with redirect_stdout(optimized_output):
            optimized_code = main(
                [str(path), "--propagate-constants"]
            )
        self.assertEqual(optimized_code, 0)
        optimized_payload = json.loads(optimized_output.getvalue())
        self.assertTrue(optimized_payload["constant_propagation_applied"])

    def test_invalid_document_returns_exit_code_two(self) -> None:
        path = ROOT / "_tmp_invalid_document.json"
        path.write_text("{ this is not valid json", encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main([str(path)])
        self.assertEqual(exit_code, 2)
        self.assertIn("error", stderr.getvalue())

    def test_leaking_document_returns_exit_code_one_and_replayable_json(self) -> None:
        document = {
            "circuit": {
                "inputs": ["p", "s"],
                "nodes": [{"id": "g", "op": "and", "args": ["p", "s"]}],
                "outputs": {"decision": "g"},
            },
            "policy": {
                "public_inputs": ["p"],
                "secret_inputs": ["s"],
                "external_outputs": ["decision"],
            },
        }
        path = ROOT / "_tmp_leak_document.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.addCleanup(path.unlink, missing_ok=True)

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main([str(path)])
        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["secure"])
        pair = payload["counterexample"]
        self.assertEqual(pair["public_assignment"], {"p": True})
        self.assertEqual(pair["first_difference"]["output"], "decision")

    def test_top_level_policy_keys_are_supported(self) -> None:
        document = {
            "inputs": ["s"],
            "nodes": [],
            "outputs": {"out": "s"},
            "public_inputs": [],
            "secret_inputs": ["s"],
            "external_outputs": ["out"],
        }
        report = check_document(document)
        self.assertFalse(report.secure)


if __name__ == "__main__":
    unittest.main()
