"""Resolver test suite.

Every case builds a small, self-contained synthetic input triple (environment,
services, catalog) in a temporary directory, drives the public API, and asserts
on the actual returned structure.  Case IDs are stable and referenced by the
local verifier.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from secretrefs import InputError, build_plan, plan_exit_code  # noqa: E402
from secretrefs.loader import load_inputs  # noqa: E402


def write_inputs(
    tmp: Path, environment: dict, services: dict, catalog: dict
) -> tuple[Path, Path, Path]:
    env_path = tmp / "environment.json"
    svc_path = tmp / "services.json"
    cat_path = tmp / "catalog.json"
    env_path.write_text(json.dumps(environment), encoding="utf-8")
    svc_path.write_text(json.dumps(services), encoding="utf-8")
    cat_path.write_text(json.dumps(catalog), encoding="utf-8")
    return env_path, svc_path, cat_path


def plan_for(tmp: Path, environment: dict, services: dict, catalog: dict) -> dict:
    env_path, svc_path, cat_path = write_inputs(tmp, environment, services, catalog)
    return build_plan(load_inputs(env_path, svc_path, cat_path))


def service_plan(plan: dict, name: str) -> dict:
    for item in plan["services"]:
        if item["service"] == name:
            return item
    raise AssertionError(f"service {name} missing from plan")


def variable(service: dict, name: str) -> dict:
    for item in service["variables"]:
        if item["variable"] == name:
            return item
    raise AssertionError(f"variable {name} missing from service {service['service']}")


class LayeringTests(unittest.TestCase):
    def test_layer_order_and_source_layer(self) -> None:
        """QA-LAYER-ORDER: service > environment > common, and layer is reported."""

        environment = {
            "environment": "prod",
            "common": [
                {"variable": "A", "ref": "alpha"},
                {"variable": "B", "ref": "alpha"},
            ],
            "environment_overrides": [{"variable": "B", "ref": "alpha", "version": 2}],
        }
        services = {
            "services": ["svc"],
            "service_overrides": {
                "svc": [{"variable": "A", "ref": "alpha", "version": 3}]
            },
        }
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1, 2, 3], "current": 1}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        a = variable(svc, "A")
        b = variable(svc, "B")
        self.assertEqual(a["source_layer"], "service")
        self.assertEqual(a["version"], 3)
        self.assertEqual(b["source_layer"], "environment")
        self.assertEqual(b["version"], 2)
        self.assertEqual(a["overrides"], [{"layer": "common", "ref": "alpha", "version": None}])

    def test_same_layer_conflict_blocks_service(self) -> None:
        """QA-LAYER-CONFLICT: two declarations in one winning layer are reported."""

        environment = {"environment": "prod", "common": [], "environment_overrides": []}
        services = {
            "services": ["svc", "other"],
            "service_overrides": {
                "svc": [
                    {"variable": "DUP", "ref": "alpha", "version": 1},
                    {"variable": "DUP", "ref": "alpha", "version": 2},
                ],
                "other": [{"variable": "SAFE", "ref": "beta"}],
            },
        }
        catalog = {
            "environment": "prod",
            "services": ["svc", "other"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1, 2], "current": 1},
                {"service": "other", "ref": "beta", "versions": [1], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertFalse(svc["executable"])
        conflict = svc["conflicts"][0]
        self.assertEqual(conflict["kind"], "layer_conflict")
        self.assertEqual(conflict["variable"], "DUP")
        self.assertEqual(conflict["layer"], "service")
        self.assertEqual(len(conflict["details"]["declarations"]), 2)
        unresolved = svc["unresolved"][0]
        self.assertEqual(unresolved["reason"], "layer_conflict")
        self.assertEqual(unresolved["variable"], "DUP")
        self.assertTrue(unresolved["blocking"])

        other = service_plan(plan, "other")
        self.assertTrue(other["executable"])
        self.assertEqual(variable(other, "SAFE")["resolved_ref"], "beta")
        self.assertFalse(plan["executable"])
        self.assertEqual(plan["summary"]["blocked_services"], 1)
        self.assertEqual(plan_exit_code(plan), 1)

    def test_common_layer_conflict_reaches_every_service(self) -> None:
        """QA-LAYER-CONFLICT-COMMON: a common-layer duplicate affects all services."""

        environment = {
            "environment": "prod",
            "common": [
                {"variable": "DUP", "ref": "alpha", "version": 1},
                {"variable": "DUP", "ref": "alpha", "version": 2},
            ],
        }
        services = {"services": ["svc", "other"]}
        catalog = {
            "environment": "prod",
            "services": ["svc", "other"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1, 2], "current": 1},
                {"service": "other", "ref": "alpha", "versions": [1, 2], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        for name in ("svc", "other"):
            item = service_plan(plan, name)
            self.assertFalse(item["executable"], name)
            self.assertEqual(item["conflicts"][0]["layer"], "common", name)
        self.assertEqual(plan["summary"]["blocked_services"], 2)

    def test_identical_duplicate_still_conflicts(self) -> None:
        """QA-LAYER-CONFLICT-IDENTICAL: even equal targets are a same-layer conflict."""

        environment = {
            "environment": "prod",
            "common": [
                {"variable": "DUP", "ref": "alpha", "version": 1},
                {"variable": "DUP", "ref": "alpha", "version": 1},
            ],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1], "current": 1}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertFalse(svc["executable"])
        conflict = svc["conflicts"][0]
        declarations = conflict["details"]["declarations"]
        self.assertEqual([d["ref"] for d in declarations], ["alpha", "alpha"])

    def test_shadowed_conflict_is_reported_but_not_blocking(self) -> None:
        """QA-LAYER-CONFLICT-SHADOWED: a lower-layer duplicate cannot block a winning layer."""

        environment = {
            "environment": "prod",
            "common": [
                {"variable": "DUP", "ref": "alpha", "version": 1},
                {"variable": "DUP", "ref": "alpha", "version": 2},
            ],
        }
        services = {
            "services": ["svc"],
            "service_overrides": {"svc": [{"variable": "DUP", "ref": "alpha", "version": 3}]},
        }
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1, 2, 3], "current": 1}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertTrue(svc["executable"])
        self.assertEqual(variable(svc, "DUP")["version"], 3)
        self.assertEqual(svc["summary"]["conflicts"], 1)
        self.assertEqual(svc["summary"]["shadowed_conflicts"], 1)
        self.assertEqual(svc["shadowed_conflicts"][0]["layer"], "common")
        self.assertEqual(svc["unresolved"], [])


class CatalogIdentityTests(unittest.TestCase):
    def test_unregistered_reference_blocks_only_owning_service(self) -> None:
        """QA-IDENTITY-MISSING: independent services keep their results."""

        environment = {"environment": "prod", "common": []}
        services = {
            "services": ["svc", "healthy"],
            "service_overrides": {
                "svc": [{"variable": "A", "ref": "missing"}],
                "healthy": [{"variable": "B", "ref": "present"}],
            },
        }
        catalog = {
            "environment": "prod",
            "services": ["svc", "healthy"],
            "references": [
                {"service": "healthy", "ref": "present", "versions": [1], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        healthy = service_plan(plan, "healthy")
        self.assertFalse(svc["executable"])
        self.assertEqual(svc["unresolved"][0]["reason"], "not_registered")
        self.assertTrue(any(item["kind"] == "not_registered" for item in svc["diagnostics"]))
        self.assertTrue(healthy["executable"])
        self.assertEqual(len(healthy["variables"]), 1)
        self.assertEqual(variable(healthy, "B")["version"], 1)
        self.assertEqual(plan["summary"]["executable_services"], 1)
        self.assertEqual(plan["summary"]["blocked_services"], 1)

    def test_wrong_service_identity_is_reported(self) -> None:
        """QA-IDENTITY-SERVICE: a ref registered under another service is rejected."""

        environment = {"environment": "prod", "common": []}
        services = {
            "services": ["a", "b"],
            "service_overrides": {"a": [{"variable": "X", "ref": "only-b"}]},
        }
        catalog = {
            "environment": "prod",
            "services": ["a", "b"],
            "references": [{"service": "b", "ref": "only-b", "versions": [1], "current": 1}],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        a = service_plan(plan, "a")
        self.assertFalse(a["executable"])
        self.assertEqual(a["unresolved"][0]["reason"], "identity_mismatch")
        self.assertIn("b", a["unresolved"][0]["message"])
        self.assertFalse(plan["executable"])

    def test_ref_registered_only_in_other_environment_is_cross_environment(self) -> None:
        """QA-IDENTITY-ENV: a ref living only in another environment is rejected."""

        environment = {"environment": "prod", "common": []}
        services = {
            "services": ["svc"],
            "service_overrides": {"svc": [{"variable": "X", "ref": "staging-only"}]},
        }
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"environment": "staging", "service": "svc", "ref": "staging-only",
                 "versions": [1], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertFalse(svc["executable"])
        self.assertEqual(svc["unresolved"][0]["reason"], "cross_environment")
        self.assertIn("staging", svc["unresolved"][0]["message"])

    def test_version_pin_and_default_rule(self) -> None:
        """QA-VERSION: explicit version pin vs catalog current."""

        environment = {
            "environment": "prod",
            "common": [
                {"variable": "PINNED", "ref": "alpha", "version": 2},
                {"variable": "DEFAULT", "ref": "alpha"},
            ],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "alpha", "versions": [1, 2, 3], "current": 3}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertEqual(variable(svc, "PINNED")["version"], 2)
        self.assertEqual(variable(svc, "PINNED")["version_rule"], "explicit")
        self.assertEqual(variable(svc, "DEFAULT")["version"], 3)
        self.assertEqual(variable(svc, "DEFAULT")["version_rule"], "catalog_current")

    def test_undeclared_version_blocks(self) -> None:
        """QA-VERSION-UNAVAILABLE: a pinned version must exist in the catalog."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "alpha", "version": 9}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [{"service": "svc", "ref": "alpha", "versions": [1, 2], "current": 1}],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertFalse(svc["executable"])
        self.assertEqual(svc["unresolved"][0]["reason"], "version_unavailable")
        self.assertIn("not registered", svc["unresolved"][0]["message"])

    def test_optional_unresolved_preserves_executable(self) -> None:
        """QA-OPTIONAL: an unresolved non-required binding does not block the plan."""

        environment = {
            "environment": "prod",
            "common": [
                {"variable": "REQ", "ref": "alpha"},
                {"variable": "OPT", "ref": "missing", "required": False},
            ],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [{"service": "svc", "ref": "alpha", "versions": [1], "current": 1}],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertTrue(svc["executable"])
        self.assertEqual(variable(svc, "REQ")["version"], 1)
        self.assertEqual(svc["unresolved"][0]["variable"], "OPT")
        self.assertFalse(svc["unresolved"][0]["blocking"])
        self.assertEqual(svc["summary"]["blocking"], 0)


class AliasTests(unittest.TestCase):
    def test_alias_chain_resolves_to_base(self) -> None:
        """QA-ALIAS: aliases resolve transitively and expose the full chain."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "leaf-alias"}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "base", "versions": [1, 2], "current": 2},
                {"service": "svc", "ref": "mid-alias", "alias_of": {"ref": "base"}},
                {"service": "svc", "ref": "leaf-alias", "alias_of": {"ref": "mid-alias"}},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        x = variable(svc, "X")
        self.assertTrue(svc["executable"])
        self.assertEqual(x["resolved_ref"], "base")
        self.assertEqual(x["version"], 2)
        self.assertTrue(x["aliased"])
        self.assertEqual(
            [step["ref"] for step in x["resolution_chain"]],
            ["leaf-alias", "mid-alias", "base"],
        )

    def test_alias_cycle_reports_full_chain(self) -> None:
        """QA-ALIAS-CYCLE: a loop names every node in the traversal."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "one"}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "one", "alias_of": {"ref": "two"}},
                {"service": "svc", "ref": "two", "alias_of": {"ref": "three"}},
                {"service": "svc", "ref": "three", "alias_of": {"ref": "one"}},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertFalse(svc["executable"])
        item = svc["unresolved"][0]
        self.assertEqual(item["reason"], "alias_cycle")
        chain = [step["ref"] for step in item["resolution_chain"]]
        self.assertEqual(chain, ["one", "two", "three", "one"])
        self.assertIn(
            "prod/svc/one -> prod/svc/two -> prod/svc/three -> prod/svc/one",
            item["message"],
        )

    def test_cross_environment_alias_reports_chain(self) -> None:
        """QA-ALIAS-CROSS-ENV: alias leaving the environment is a hard failure."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "prod-alias"}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "prod-alias",
                 "alias_of": {"environment": "staging", "ref": "staging-base"}},
                {"environment": "staging", "service": "svc", "ref": "staging-base",
                 "versions": [1], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertFalse(svc["executable"])
        item = svc["unresolved"][0]
        self.assertEqual(item["reason"], "cross_environment")
        chain = [(step["environment"], step["service"], step["ref"])
                 for step in item["resolution_chain"]]
        self.assertEqual(
            chain, [("prod", "svc", "prod-alias"), ("staging", "svc", "staging-base")]
        )
        self.assertIn("staging/svc/staging-base", item["message"])

    def test_version_pin_travels_through_alias(self) -> None:
        """QA-ALIAS-VERSION: a pinned version is applied at the end of the alias chain."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "alias-ref", "version": 1}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "base", "versions": [1, 2, 3], "current": 3},
                {"service": "svc", "ref": "alias-ref", "alias_of": {"ref": "base"}},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        x = variable(service_plan(plan, "svc"), "X")
        self.assertEqual(x["resolved_ref"], "base")
        self.assertEqual(x["version"], 1)
        self.assertEqual(x["version_rule"], "explicit")

    def test_alias_to_other_service_identity_is_rejected(self) -> None:
        """QA-ALIAS-IDENTITY: an alias may not jump to another service's identity."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "api-alias"}],
        }
        services = {"services": ["worker"]}
        catalog = {
            "environment": "prod",
            "services": ["worker", "api"],
            "references": [
                {"service": "worker", "ref": "api-alias",
                 "alias_of": {"service": "api", "ref": "api-secret"}},
                {"service": "api", "ref": "api-secret", "versions": [1], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "worker")
        self.assertFalse(svc["executable"])
        item = svc["unresolved"][0]
        self.assertEqual(item["reason"], "identity_mismatch")
        self.assertIn("prod/api/api-secret", item["message"])

    def test_dangling_alias(self) -> None:
        """QA-ALIAS-DANGLING: alias to an unregistered ref names the target."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "X", "ref": "broken"}],
        }
        services = {"services": ["svc"]}
        catalog = {
            "environment": "prod",
            "services": ["svc"],
            "references": [
                {"service": "svc", "ref": "broken", "alias_of": {"ref": "absent"}}
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        svc = service_plan(plan, "svc")
        self.assertEqual(svc["unresolved"][0]["reason"], "dangling_alias")
        self.assertIn("absent", svc["unresolved"][0]["message"])


class SameNameTests(unittest.TestCase):
    def test_same_name_different_source_is_surfaced(self) -> None:
        """QA-SAME-NAME: a shared variable fed by different references is flagged."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "DB_PASSWORD", "ref": "shared-db"}],
        }
        services = {
            "services": ["api", "worker"],
            "service_overrides": {
                "api": [{"variable": "DB_PASSWORD", "ref": "shared-db", "version": 2}],
            },
        }
        catalog = {
            "environment": "prod",
            "services": ["api", "worker"],
            "references": [
                {"service": "api", "ref": "shared-db", "versions": [1, 2], "current": 2},
                {"service": "worker", "ref": "shared-db", "versions": [1, 2], "current": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        self.assertEqual(len(plan["same_name_collisions"]), 1)
        collision = plan["same_name_collisions"][0]
        self.assertEqual(collision["variable"], "DB_PASSWORD")
        self.assertEqual(collision["distinct_targets"], 2)
        by_service = {item["service"]: item for item in collision["services"]}
        self.assertEqual(by_service["api"]["version"], 2)
        self.assertEqual(by_service["worker"]["version"], 1)
        self.assertEqual(by_service["api"]["source_layer"], "service")
        self.assertEqual(by_service["worker"]["source_layer"], "common")

    def test_same_name_no_collision_when_uniform(self) -> None:
        """QA-SAME-NAME-UNIFORM: identical ref+version for all services is not a collision."""

        environment = {
            "environment": "prod",
            "common": [{"variable": "DB_PASSWORD", "ref": "shared-db"}],
        }
        services = {"services": ["api", "worker"]}
        catalog = {
            "environment": "prod",
            "services": ["api", "worker"],
            "references": [
                {"service": "api", "ref": "shared-db", "versions": [1, 2], "current": 2},
                {"service": "worker", "ref": "shared-db", "versions": [1, 2], "current": 2},
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            plan = plan_for(Path(raw), environment, services, catalog)

        self.assertEqual(plan["same_name_collisions"], [])
        uniform = {e["variable"]: e for e in plan["same_name_variables"]}["DB_PASSWORD"]
        self.assertTrue(uniform["uniform"])
        self.assertEqual(uniform["distinct_targets"], 1)


class InputValidationTests(unittest.TestCase):
    def test_missing_file_is_input_error(self) -> None:
        """QA-INPUT-FILE: absent files produce InputError, not a plan."""

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(InputError) as caught:
                load_inputs(
                    Path(raw) / "nope.json",
                    Path(raw) / "services.json",
                    Path(raw) / "catalog.json",
                )
        self.assertIn("cannot read", str(caught.exception))

    def test_environment_mismatch_is_input_error(self) -> None:
        """QA-INPUT-ENV: environment file and catalog must agree on environment."""

        with tempfile.TemporaryDirectory() as raw:
            env_path, svc_path, cat_path = write_inputs(
                Path(raw),
                {"environment": "prod", "common": []},
                {"services": ["svc"]},
                {"environment": "staging", "services": ["svc"], "references": []},
            )
            with self.assertRaises(InputError) as caught:
                load_inputs(env_path, svc_path, cat_path)
        self.assertIn("does not match", str(caught.exception))

    def test_declared_service_not_in_catalog_is_input_error(self) -> None:
        """QA-INPUT-SERVICES: undeclared catalog services are rejected."""

        with tempfile.TemporaryDirectory() as raw:
            env_path, svc_path, cat_path = write_inputs(
                Path(raw),
                {"environment": "prod", "common": []},
                {"services": ["svc", "ghost"]},
                {"environment": "prod", "services": ["svc"], "references": []},
            )
            with self.assertRaises(InputError) as caught:
                load_inputs(env_path, svc_path, cat_path)
        self.assertIn("not registered", str(caught.exception))

    def test_service_override_for_undeclared_service_is_input_error(self) -> None:
        """QA-INPUT-OVERRIDE: overrides cannot target a service not in this deployment."""

        with tempfile.TemporaryDirectory() as raw:
            env_path, svc_path, cat_path = write_inputs(
                Path(raw),
                {"environment": "prod", "common": []},
                {
                    "services": ["svc"],
                    "service_overrides": {"ghost": [{"variable": "X", "ref": "a"}]},
                },
                {"environment": "prod", "services": ["svc"], "references": []},
            )
            with self.assertRaises(InputError) as caught:
                load_inputs(env_path, svc_path, cat_path)
        self.assertIn("undeclared services", str(caught.exception))

    def test_alias_with_versions_is_input_error(self) -> None:
        """QA-INPUT-ALIAS: an alias entry may not also carry versions."""

        with tempfile.TemporaryDirectory() as raw:
            env_path, svc_path, cat_path = write_inputs(
                Path(raw),
                {"environment": "prod", "common": []},
                {"services": ["svc"]},
                {
                    "environment": "prod",
                    "services": ["svc"],
                    "references": [
                        {"service": "svc", "ref": "bad", "versions": [1],
                         "alias_of": {"ref": "x"}}
                    ],
                },
            )
            with self.assertRaises(InputError) as caught:
                load_inputs(env_path, svc_path, cat_path)
        self.assertIn("both versions and alias_of", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
