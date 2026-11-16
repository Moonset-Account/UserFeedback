"""RED tests for V8n explicit fetch persistence."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import DownloadedArtifact, persist_downloaded_artifact


class TestV8nExplicitFetch(unittest.TestCase):
    def test_persists_payload_and_metadata_only_provenance_atomically(self):
        payload = b'{"records": [{"id": "OSV-V8N-1"}]}\n'
        artifact = DownloadedArtifact(
            payload=payload,
            requested_url="https://updates.example.test/osv.json",
            final_url="https://cdn.example.test/osv.json",
            etag='"v3"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            content_type="application/json",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "osv.json"
            provenance = Path(directory) / "osv.provenance.json"
            report = persist_downloaded_artifact(artifact, output, provenance, "osv")
            self.assertEqual(output.read_bytes(), payload)
            metadata = json.loads(provenance.read_text(encoding="utf-8"))
            self.assertEqual(report["state"], "downloaded")
            self.assertEqual(metadata["payload_sha256"], "sha256:" + hashlib.sha256(payload).hexdigest())
            self.assertEqual(metadata["final_url"], artifact.final_url)
            self.assertNotIn("payload", metadata)
            self.assertNotIn("records", metadata)
            self.assertEqual(report["provenance_sha256"], metadata["provenance_sha256"])

    def test_rejects_not_modified_without_overwriting_existing_output(self):
        artifact = DownloadedArtifact(
            payload=None,
            requested_url="https://updates.example.test/osv.json",
            final_url="https://updates.example.test/osv.json",
            etag='"v1"',
            last_modified=None,
            content_type="application/json",
            not_modified=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "osv.json"
            output.write_bytes(b"old")
            with self.assertRaises(ValueError):
                persist_downloaded_artifact(artifact, output, Path(directory) / "provenance.json", "osv")
            self.assertEqual(output.read_bytes(), b"old")

    def test_rejects_invalid_json_and_symlink_destinations(self):
        artifact = DownloadedArtifact(
            payload=b"not-json",
            requested_url="https://updates.example.test/osv.json",
            final_url="https://updates.example.test/osv.json",
            etag=None,
            last_modified=None,
            content_type="application/json",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "osv.json"
            with self.assertRaises(ValueError):
                persist_downloaded_artifact(artifact, output, Path(directory) / "provenance.json", "osv")
            real = Path(directory) / "real.json"
            real.write_bytes(b"old")
            output.symlink_to(real)
            valid = DownloadedArtifact(**{**artifact.__dict__, "payload": b"{}"})
            with self.assertRaises(ValueError):
                persist_downloaded_artifact(valid, output, Path(directory) / "provenance.json", "osv")


if __name__ == "__main__":
    unittest.main()
