"""RED tests for the V2a Python/Node inventory batch."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.inventory import InventoryResult, UnresolvedDependency, build_inventory
from src.vulnerability.parsers.package_lock import parse_package_lock
from src.vulnerability.parsers.requirements import parse_requirements


class TestV2aRequirements(unittest.TestCase):
    def test_exact_requirement_becomes_component_with_purl(self):
        components, unresolved, warnings = parse_requirements(
            "# comment\nrequests==2.31.0\n"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].purl, "pkg:pypi/requests@2.31.0")
        self.assertTrue(components[0].exact_version)
        self.assertEqual(components[0].manifest_path, "requirements.txt")

    def test_range_requirement_is_unresolved_not_a_confirmed_component(self):
        components, unresolved, warnings = parse_requirements("flask>=2.0\n")
        self.assertEqual(components, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].name, "flask")
        self.assertEqual(unresolved[0].reason, "version_range")

    def test_include_and_malformed_lines_are_bounded_warnings(self):
        components, unresolved, warnings = parse_requirements(
            "-r nested.txt\nthis is not a requirement\n"
        )
        self.assertEqual(components, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(len(warnings), 2)


class TestV2aPackageLock(unittest.TestCase):
    def test_package_lock_v3_packages_are_exact_components(self):
        payload = {
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"express": "4.18.2"}},
                "node_modules/express": {"version": "4.18.2", "resolved": "https://registry.npmjs.org/express"},
            },
        }
        components, unresolved, warnings = parse_package_lock(json.dumps(payload))
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].purl, "pkg:npm/express@4.18.2")
        self.assertTrue(components[0].exact_version)

    def test_package_lock_v1_dependencies_are_supported_without_network(self):
        payload = {"lockfileVersion": 1, "dependencies": {"left-pad": {"version": "1.3.0"}}}
        components, unresolved, warnings = parse_package_lock(json.dumps(payload))
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual(components[0].purl, "pkg:npm/left-pad@1.3.0")

    def test_invalid_package_lock_is_reported(self):
        with self.assertRaises(ValueError):
            parse_package_lock("[]")


class TestV2aInventory(unittest.TestCase):
    def test_build_inventory_reads_manifests_without_executing_repository_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
            (root / "package-lock.json").write_text(
                json.dumps({"lockfileVersion": 3, "packages": {"node_modules/lodash": {"version": "4.17.21"}}}),
                encoding="utf-8",
            )
            (root / "setup.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
            result = build_inventory(root)
        self.assertIsInstance(result, InventoryResult)
        self.assertEqual(len(result.components), 2)
        self.assertEqual(result.unresolved, [])
        self.assertEqual(result.warnings, [])
        self.assertEqual({component.ecosystem for component in result.components}, {"pypi", "npm"})

    def test_unsupported_manifest_is_warning_and_does_not_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
            result = build_inventory(root)
        self.assertEqual(len(result.components), 1)
        self.assertTrue(any("pyproject.toml" in warning for warning in result.warnings))


if __name__ == "__main__":
    unittest.main()
