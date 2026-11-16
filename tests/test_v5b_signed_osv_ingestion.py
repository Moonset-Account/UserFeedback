"""RED tests for V5b signed OSV envelope verification."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.ingestion import ingest_osv_file

PUBLIC_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
SIGNED_ENVELOPE = {
    "schema": "coderisktools.vulnerability.signed-feed",
    "version": 1,
    "key_id": "osv-fixture",
    "payload": {"vulns": [{"id": "OSV-SIGNED-TEST", "affected": [{"package": {"ecosystem": "PyPI", "name": "requests"}}]}]},
    "signature": "c5c12c4507fdd40240542bed8f44e61c99752cced523bacf1ca8fd400eb0fafbb9f4e40490a7bec4fac0a79c887cf6cfa229b20670ae7e413baaa312b5244007",
}


def keyring(path: Path, public_key: str = PUBLIC_KEY):
    path.write_text(json.dumps({"schema": "coderisktools.rule-keyring", "version": 1, "keys": {"osv-fixture": public_key}}), encoding="utf-8")


class TestV5bSignedOSVIngestion(unittest.TestCase):
    def test_valid_signed_envelope_is_verified_and_provenance_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "signed.json"; ring = root / "keyring.json"
            feed.write_text(json.dumps(SIGNED_ENVELOPE, separators=(",", ":")), encoding="utf-8"); keyring(ring)
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "signed-1", "osv-fixture", keyring_path=str(ring), activate=True)
            self.assertEqual(report.signature_status, "verified")
            self.assertEqual(report.signing_key_id, "osv-fixture")
            self.assertEqual(report.state, "active")
            self.assertEqual(db.active_snapshot()["manifest"]["signature_status"], "verified")

    def test_unsigned_feed_is_rejected_when_keyring_verification_is_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); feed = root / "unsigned.json"; ring = root / "keyring.json"
            feed.write_text(json.dumps(SIGNED_ENVELOPE["payload"]), encoding="utf-8"); keyring(ring)
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "unsigned", "fixture", keyring_path=str(ring), activate=True)
            self.assertEqual(report.signature_status, "unsigned")
            self.assertEqual(report.state, "rejected")
            self.assertEqual(db.advisory_count(), 0)

    def test_untrusted_and_tampered_envelopes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ring = root / "keyring.json"; keyring(ring, "00" * 32)
            untrusted_payload = dict(SIGNED_ENVELOPE); untrusted_payload["key_id"] = "other-key"
            untrusted = root / "untrusted.json"; untrusted.write_text(json.dumps(untrusted_payload), encoding="utf-8")
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(untrusted), db, "untrusted", "fixture", keyring_path=str(ring))
            self.assertEqual(report.signature_status, "untrusted")
            tampered = dict(SIGNED_ENVELOPE); tampered["payload"] = {"vulns": []}
            tampered_path = root / "tampered.json"; tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            report = ingest_osv_file(str(tampered_path), db, "tampered", "fixture", keyring_path=str(root / "keyring.json"))
            self.assertEqual(report.signature_status, "invalid")
            self.assertEqual(db.advisory_count(), 0)

    def test_signed_envelope_rejects_duplicate_or_extra_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ring = root / "keyring.json"; keyring(ring)
            bad = dict(SIGNED_ENVELOPE); bad["extra"] = True
            feed = root / "bad.json"; feed.write_text(json.dumps(bad), encoding="utf-8")
            db = VulnerabilityDatabase(str(root / "vulnerability.sqlite"))
            report = ingest_osv_file(str(feed), db, "bad", "fixture", keyring_path=str(ring))
            self.assertEqual(report.signature_status, "invalid")
            self.assertEqual(report.state, "rejected")


if __name__ == "__main__":
    unittest.main()
