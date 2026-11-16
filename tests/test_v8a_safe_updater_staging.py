"""RED tests for V8a safe offline updater staging."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import StageLimits, stage_json_artifact, verify_staged_artifact


class TestV8aSafeUpdaterStaging(unittest.TestCase):
    def test_stages_deterministic_envelope_and_verifies_hash(self):
        payload = b'{"b": 2, "a": 1}'
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "osv.stage.json"
            first = stage_json_artifact(payload, destination, "osv")
            first_bytes = destination.read_bytes()
            second = stage_json_artifact(payload, destination, "osv")
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, destination.read_bytes())
            self.assertEqual(first["source_sha256"], "sha256:" + hashlib.sha256(payload).hexdigest())
            self.assertEqual(verify_staged_artifact(destination), first)

    def test_rejects_invalid_json_and_oversized_payload_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bad.stage.json"
            with self.assertRaises(ValueError):
                stage_json_artifact(b"{not-json", destination, "osv")
            with self.assertRaises(ValueError):
                stage_json_artifact(b"{}", destination, "osv", StageLimits(max_bytes=1))
            self.assertFalse(destination.exists())

    def test_rejects_invalid_source_id_and_tampered_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "osv.stage.json"
            with self.assertRaises(ValueError):
                stage_json_artifact(b"{}", destination, "../osv")
            stage_json_artifact(b"{}", destination, "osv")
            data = json.loads(destination.read_text(encoding="utf-8"))
            data["payload"] = {"changed": True}
            destination.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                verify_staged_artifact(destination)

    def test_limits_record_count_for_top_level_lists(self):
        payload = json.dumps([{"id": 1}, {"id": 2}]).encode()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "records.stage.json"
            with self.assertRaises(ValueError):
                stage_json_artifact(payload, destination, "fixture", StageLimits(max_records=1))


if __name__ == "__main__":
    unittest.main()
