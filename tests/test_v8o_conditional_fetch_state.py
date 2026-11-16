"""RED tests for V8o verified conditional fetch state."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.updater import (
    DownloadedArtifact,
    FetchConditions,
    build_source_provenance,
    load_fetch_conditions,
)


class TestV8oConditionalFetchState(unittest.TestCase):
    def _write(self, directory: Path, artifact: DownloadedArtifact) -> Path:
        path = directory / "provenance.json"
        path.write_text(json.dumps(build_source_provenance("osv", artifact), sort_keys=True), encoding="utf-8")
        return path

    def test_loads_only_verified_etag_and_last_modified(self):
        artifact = DownloadedArtifact(b"{}", "https://updates.example.test/a", "https://updates.example.test/a", '"v4"', "Wed, 01 Jan 2025 00:00:00 GMT", "application/json")
        with tempfile.TemporaryDirectory() as directory:
            conditions = load_fetch_conditions(self._write(Path(directory), artifact))
            self.assertEqual(conditions, FetchConditions(etag='"v4"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT"))

    def test_rejects_tampered_or_wrong_schema_before_network_use(self):
        artifact = DownloadedArtifact(b"{}", "https://updates.example.test/a", "https://updates.example.test/a", '"v4"', None, "application/json")
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(Path(directory), artifact)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["etag"] = '"tampered"'
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_fetch_conditions(path)
            path.write_text(json.dumps({"schema": "wrong", "version": 1}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_fetch_conditions(path)

    def test_rejects_symlink_and_missing_condition_file(self):
        artifact = DownloadedArtifact(b"{}", "https://updates.example.test/a", "https://updates.example.test/a", None, None, "application/json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = self._write(root, artifact)
            link = root / "link.json"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                load_fetch_conditions(link)
            with self.assertRaises(FileNotFoundError):
                load_fetch_conditions(root / "missing.json")


if __name__ == "__main__":
    unittest.main()
