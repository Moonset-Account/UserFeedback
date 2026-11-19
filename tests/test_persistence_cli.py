"""Persistence and CLI acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

from helpers import WORKSPACE, at

from revocation_store import Reason, RevocationStore, Status, Table, TableEntry


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, "state.sqlite3")

    def test_state_survives_reopen(self) -> None:
        with RevocationStore(self.path) as store:
            store.ingest(
                Table.full(
                    issuer="Example CA",
                    scope="root",
                    number=1,
                    this_update=at(0),
                    next_update=at(48),
                    entries=[TableEntry("A1", Reason.KEY_COMPROMISE, at(1))],
                )
            )
        with RevocationStore(self.path) as reopened:
            result = reopened.lookup("Example CA", "root", "A1", at(2))
            self.assertEqual(result.status, Status.REVOKED)
            result = reopened.lookup("Example CA", "root", "Z9", at(2))
            self.assertEqual(result.status, Status.NOT_LISTED)

    def test_reopen_keeps_identity_conflict_detection(self) -> None:
        table = Table.full(
            issuer="Example CA", scope="root", number=1, this_update=at(0), next_update=at(48)
        )
        with RevocationStore(self.path) as store:
            store.ingest(table)
        with RevocationStore(self.path) as store:
            again = store.ingest(table)
            self.assertFalse(again.inserted)


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKSPACE
    return subprocess.run(
        [sys.executable, "-m", "revocation_store", *args],
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
    )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db = os.path.join(self.tmpdir.name, "cli.sqlite3")

    def _write(self, name: str, payload) -> str:
        path = os.path.join(self.tmpdir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def _table_json(self, **overrides):
        payload = {
            "kind": "FULL",
            "issuer": "Example CA",
            "scope": "root",
            "number": 1,
            "this_update": at(0).isoformat().replace("+00:00", "Z"),
            "next_update": at(48).isoformat().replace("+00:00", "Z"),
            "entries": [
                {
                    "serial": "A1",
                    "reason": "keyCompromise",
                    "revocation_time": at(1).isoformat().replace("+00:00", "Z"),
                }
            ],
        }
        payload.update(overrides)
        return payload

    def test_ingest_then_lookup_round_trip(self) -> None:
        path = self._write("full.json", self._table_json())
        ingest = _run_cli("--db", self.db, "ingest", "--file", path)
        self.assertEqual(ingest.returncode, 0, ingest.stderr)
        self.assertEqual(json.loads(ingest.stdout)["count"], 1)

        lookup = _run_cli(
            "--db", self.db,
            "lookup", "--issuer", "Example CA", "--scope", "root",
            "--serial", "A1", "--at", at(2).isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(lookup.returncode, 0, lookup.stderr)
        payload = json.loads(lookup.stdout)
        self.assertEqual(payload["status"], "revoked")
        self.assertEqual(payload["reason"], "keyCompromise")

    def test_lookup_returns_unknown_without_evidence(self) -> None:
        lookup = _run_cli(
            "--db", self.db,
            "lookup", "--issuer", "Example CA", "--scope", "root",
            "--serial", "A1", "--at", at(2).isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(lookup.returncode, 0, lookup.stderr)
        self.assertEqual(json.loads(lookup.stdout)["status"], "unknown")

    def test_cli_rejects_malformed_table(self) -> None:
        path = self._write("bad.json", self._table_json(next_update=at(-5).isoformat()))
        ingest = _run_cli("--db", self.db, "ingest", "--file", path)
        self.assertEqual(ingest.returncode, 1)
        payload = json.loads(ingest.stdout)
        self.assertEqual(payload["error"], "invalid_table")
        # Nothing was written.
        listing = _run_cli("--db", self.db, "tables")
        self.assertEqual(json.loads(listing.stdout), [])

    def test_cli_rejects_conflicting_identity(self) -> None:
        first = self._write("first.json", self._table_json())
        self.assertEqual(_run_cli("--db", self.db, "ingest", "--file", first).returncode, 0)
        conflict = self._write(
            "conflict.json", self._table_json(entries=[])
        )
        ingest = _run_cli("--db", self.db, "ingest", "--file", conflict)
        self.assertEqual(ingest.returncode, 1)
        self.assertEqual(json.loads(ingest.stdout)["error"], "conflict")

    def test_cli_lists_tables_and_entries(self) -> None:
        path = self._write("full.json", self._table_json())
        _run_cli("--db", self.db, "ingest", "--file", path)
        tables = json.loads(_run_cli("--db", self.db, "tables").stdout)
        self.assertEqual(len(tables), 1)
        table_id = tables[0]["table_id"]
        entries = json.loads(
            _run_cli("--db", self.db, "entries", "--table-id", str(table_id)).stdout
        )
        self.assertEqual(entries["entries"][0]["serial"], "A1")


if __name__ == "__main__":
    unittest.main()
