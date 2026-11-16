"""RED tests for the V2c Poetry/uv inventory batch."""

import tempfile
import unittest
from pathlib import Path

from src.vulnerability.inventory import build_inventory
from src.vulnerability.parsers.poetry_lock import parse_poetry_lock
from src.vulnerability.parsers.uv_lock import parse_uv_lock


class TestV2cPoetry(unittest.TestCase):
    def test_poetry_lock_packages_become_exact_components(self):
        text = '''[[package]]
name = "requests"
version = "2.31.0"
description = "HTTP library"
category = "main"
optional = false
python-versions = ">=3.7"

[[package]]
name = "pytest"
version = "8.2.2"
groups = ["dev"]

[metadata]
lock-version = "2.0"
'''
        components, unresolved, warnings = parse_poetry_lock(text)
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual({c.purl for c in components}, {
            "pkg:pypi/requests@2.31.0",
            "pkg:pypi/pytest@8.2.2",
        })
        self.assertEqual(components[0].source_type, "poetry-lock")
        self.assertTrue(all(component.exact_version for component in components))

    def test_poetry_lock_missing_version_is_warning(self):
        text = '[[package]]\nname = "local-project"\ncategory = "main"\n'
        components, unresolved, warnings = parse_poetry_lock(text)
        self.assertEqual(components, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(len(warnings), 1)


class TestV2cUv(unittest.TestCase):
    def test_uv_lock_packages_become_exact_components(self):
        text = '''version = 1
requires-python = ">=3.11"

[[package]]
name = "anyio"
version = "4.4.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "demo"
version = "0.1.0"
source = { editable = "." }
'''
        components, unresolved, warnings = parse_uv_lock(text)
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual({c.purl for c in components}, {
            "pkg:pypi/anyio@4.4.0",
            "pkg:pypi/demo@0.1.0",
        })

    def test_uv_lock_missing_package_version_is_warning(self):
        text = 'version = 1\n\n[[package]]\nname = "workspace"\n'
        components, unresolved, warnings = parse_uv_lock(text)
        self.assertEqual(components, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(len(warnings), 1)


class TestV2cInventory(unittest.TestCase):
    def test_build_inventory_reads_poetry_and_uv_without_executing_project_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "poetry.lock").write_text(
                '[[package]]\nname = "requests"\nversion = "2.31.0"\n',
                encoding="utf-8",
            )
            (root / "uv.lock").write_text(
                'version = 1\n\n[[package]]\nname = "anyio"\nversion = "4.4.0"\n',
                encoding="utf-8",
            )
            (root / "setup.py").write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
            result = build_inventory(root)
        self.assertEqual({component.purl for component in result.components}, {
            "pkg:pypi/requests@2.31.0",
            "pkg:pypi/anyio@4.4.0",
        })
        self.assertEqual(result.unresolved, [])
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
