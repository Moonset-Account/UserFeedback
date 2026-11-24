from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from authorization_checker import (  # noqa: E402
    Identity,
    IdentityConflictError,
    PersonRegistry,
    Scope,
    check_authorization,
    evaluate_authorization,
)


class PublicApiAndRegistryTests(unittest.TestCase):
    def test_check_authorization_alias_is_public_api(self) -> None:
        self.assertIs(check_authorization, evaluate_authorization)

    def test_keyword_api_with_numeric_intervals(self) -> None:
        decision = evaluate_authorization(
            target_service="svc",
            project_id="proj",
            start=0,
            end=10,
            approvals=[
                {
                    "id": "bo",
                    "type": "business_owner",
                    "person_id": "a",
                    "start": 0,
                    "end": 10,
                },
                {
                    "id": "ac",
                    "type": "custodian",
                    "person_id": "b",
                    "start": 2,
                    "end": 8,
                },
            ],
        )
        self.assertTrue(decision.grantable)
        self.assertEqual((decision.grants[0].start, decision.grants[0].end), (2, 8))

    def test_registry_accepts_person_objects_and_detects_conflicts(self) -> None:
        registry = PersonRegistry(
            [
                {
                    "person_id": "alice",
                    "identities": [
                        Identity("corp", "alice"),
                        "external:alice@example.test",
                    ],
                },
                ("bob", "corp", "bob"),
            ]
        )
        self.assertEqual(
            registry.person_for(Identity("external", "alice@example.test")),
            "alice",
        )
        self.assertEqual(registry.person_for("corp:bob"), "bob")

        with self.assertRaises(IdentityConflictError):
            registry.bind("carol", "corp:bob")

    def test_jurisdiction_constraint_must_match_request(self) -> None:
        scope = Scope.of(project="p", service="s", jurisdiction="CN")
        self.assertTrue(
            scope.covers(project_id="p", service_id="s", jurisdiction_id="CN")
        )
        self.assertFalse(
            scope.covers(project_id="p", service_id="s", jurisdiction_id="US")
        )
        self.assertFalse(scope.covers(project_id="p", service_id="s"))

    def test_stable_selected_pair_does_not_switch_to_optional_alternative(self) -> None:
        decision = evaluate_authorization(
            target_service="s",
            project_id="p",
            start=0,
            end=10,
            approvals=[
                {"id": "bo-a", "type": "bo", "person_id": "a", "start": 0, "end": 10},
                {"id": "ac-b", "type": "ac", "person_id": "b", "start": 0, "end": 10},
                # An alternative custodian starts later but should not cause the
                # primary timeline to switch while b remains valid.
                {"id": "ac-c", "type": "ac", "person_id": "c", "start": 5, "end": 10},
            ],
        )
        self.assertEqual(len(decision.grants), 1)
        self.assertEqual(
            decision.grants[0].responsible.account_custodian.person_id, "b"
        )
        pair_keys = {
            g.responsible.account_custodian.person_id
            for g in decision.feasible_pair_intervals
        }
        self.assertEqual(pair_keys, {"b", "c"})

    def test_example_files_run_through_cli(self) -> None:
        interleaving = ROOT / "examples" / "interleaving_request.json"
        completed = subprocess.run(
            [sys.executable, "-m", "authorization_checker", str(interleaving)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["fully_authorized"])
        self.assertEqual(len(payload["grants"]), 2)

        same_person = ROOT / "examples" / "same_person_missing_category.json"
        completed = subprocess.run(
            [sys.executable, "-m", "authorization_checker", str(same_person)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["grants"], [])
        self.assertEqual(
            payload["primary_missing_independent_approval_type"],
            "account_custodian",
        )


if __name__ == "__main__":
    unittest.main()
