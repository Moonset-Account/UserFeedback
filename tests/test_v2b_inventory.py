"""RED tests for the V2b Cargo/Go/Composer inventory batch."""

import json
import tempfile
import unittest
from pathlib import Path

from src.vulnerability.inventory import build_inventory
from src.vulnerability.parsers.cargo_lock import parse_cargo_lock
from src.vulnerability.parsers.composer_lock import parse_composer_lock
from src.vulnerability.parsers.go_mod import parse_go_mod
from src.vulnerability.parsers.go_sum import parse_go_sum


class TestV2bCargo(unittest.TestCase):
    def test_cargo_lock_package_stanzas_become_exact_components(self):
        text = '''version = 3

[[package]]
name = "serde"
version = "1.0.203"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "abc"

[[package]]
name = "my-app"
version = "0.1.0"
'''
        components, unresolved, warnings = parse_cargo_lock(text)
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual([c.purl for c in components], [
            "pkg:cargo/serde@1.0.203",
            "pkg:cargo/my-app@0.1.0",
        ])

    def test_cargo_lock_malformed_stanza_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_cargo_lock('version = 3\n[[package]]\nname = "serde"\n')


class TestV2bGo(unittest.TestCase):
    def test_go_mod_require_block_becomes_exact_components(self):
        text = '''module example.com/app

go 1.22

require (
    github.com/pkg/errors v0.9.1
    golang.org/x/text v0.15.0 // indirect
)
'''
        components, unresolved, warnings = parse_go_mod(text)
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual({c.purl for c in components}, {
            "pkg:golang/github.com/pkg/errors@v0.9.1",
            "pkg:golang/golang.org/x/text@v0.15.0",
        })

    def test_go_sum_ignores_go_mod_checksum_rows_and_deduplicates(self):
        text = '''github.com/pkg/errors v0.9.1 h1:abc
 github.com/pkg/errors v0.9.1/go.mod h1:def
 github.com/pkg/errors v0.9.1 h1:abc
'''
        components, unresolved, warnings = parse_go_sum(text)
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].purl, "pkg:golang/github.com/pkg/errors@v0.9.1")

    def test_go_mod_invalid_version_line_is_warning(self):
        components, unresolved, warnings = parse_go_mod("require example.com/no-version\n")
        self.assertEqual(components, [])
        self.assertEqual(unresolved, [])
        self.assertEqual(len(warnings), 1)


    def test_build_inventory_deduplicates_go_mod_and_go_sum_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "go.mod").write_text(
                "module example.com/app\n\nrequire github.com/pkg/errors v0.9.1\n",
                encoding="utf-8",
            )
            (root / "go.sum").write_text(
                "github.com/pkg/errors v0.9.1 h1:abc\n",
                encoding="utf-8",
            )
            result = build_inventory(root)
        self.assertEqual(len(result.components), 1)
        self.assertEqual(result.components[0].purl, "pkg:golang/github.com/pkg/errors@v0.9.1")


class TestV2bComposer(unittest.TestCase):
    def test_composer_lock_combines_production_and_dev_packages(self):
        text = json.dumps({
            "packages": [{"name": "symfony/console", "version": "v7.0.7"}],
            "packages-dev": [{"name": "phpunit/phpunit", "version": "11.1.3"}],
        })
        components, unresolved, warnings = parse_composer_lock(text)
        self.assertEqual(unresolved, [])
        self.assertEqual(warnings, [])
        self.assertEqual({c.purl for c in components}, {
            "pkg:composer/symfony/console@7.0.7",
            "pkg:composer/phpunit/phpunit@11.1.3",
        })


class TestV2bInventory(unittest.TestCase):
    def test_build_inventory_adds_cargo_go_and_composer_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.lock").write_text(
                'version = 3\n\n[[package]]\nname = "serde"\nversion = "1.0.203"\n',
                encoding="utf-8",
            )
            (root / "go.mod").write_text(
                'module example.com/app\n\nrequire github.com/pkg/errors v0.9.1\n',
                encoding="utf-8",
            )
            (root / "composer.lock").write_text(
                json.dumps({"packages": [{"name": "monolog/monolog", "version": "3.6.0"}]}),
                encoding="utf-8",
            )
            result = build_inventory(root)
        self.assertEqual({component.ecosystem for component in result.components}, {"cargo", "golang", "composer"})
        self.assertEqual(len(result.components), 3)
        self.assertEqual(result.unresolved, [])
        self.assertEqual(result.warnings, [])


if __name__ == "__main__":
    unittest.main()
