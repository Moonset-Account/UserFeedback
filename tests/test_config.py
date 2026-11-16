"""Unit tests for severity configuration."""

import json
import os
import tempfile
import unittest
from src.config import ScannerConfig, load_config, apply_overrides, default_config
from src.patterns import SecretPattern, DEFAULT_SECRET_PATTERNS


class TestScannerConfig(unittest.TestCase):
    """Test ScannerConfig data class."""

    def test_default_config(self):
        config = default_config()
        self.assertEqual(config.fail_on_severity, "medium")
        self.assertEqual(config.warn_on_severity, "low")
        self.assertEqual(len(config.pattern_overrides), 0)
        self.assertEqual(len(config.custom_patterns), 0)


class TestLoadConfig(unittest.TestCase):
    """Test loading configuration from JSON files."""

    def test_load_nonexistent_file(self):
        config = load_config("/nonexistent/path/config.json")
        self.assertEqual(config.fail_on_severity, "medium")
        self.assertEqual(config.warn_on_severity, "low")

    def test_load_valid_config(self):
        config_data = {
            "thresholds": {
                "fail_on_severity": "high",
                "warn_on_severity": "medium"
            },
            "pattern_overrides": {
                "GOOGLE_API_KEY": {"severity": "low"}
            },
            "custom_patterns": [
                {
                    "name": "MY_COMPANY_KEY",
                    "regex": "myco_[a-zA-Z0-9]{32}",
                    "severity": "critical"
                }
            ],
            "file_patterns": {
                "high_severity_paths": ["**/auth/**"],
                "medium_severity_paths": ["**/config/**"],
                "ignore_paths": ["**/test/**"]
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_data, f)
            f.flush()
            config = load_config(f.name)

        os.unlink(f.name)

        self.assertEqual(config.fail_on_severity, "high")
        self.assertEqual(config.warn_on_severity, "medium")
        self.assertEqual(len(config.pattern_overrides), 1)
        self.assertEqual(len(config.custom_patterns), 1)
        self.assertEqual(config.custom_patterns[0].name, "MY_COMPANY_KEY")
        self.assertEqual(config.high_severity_paths, ["**/auth/**"])
        self.assertEqual(config.ignore_paths, ["**/test/**"])

    def test_load_empty_config(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            f.flush()
            config = load_config(f.name)

        os.unlink(f.name)

        self.assertEqual(config.fail_on_severity, "medium")
        self.assertEqual(config.warn_on_severity, "low")

    def test_load_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            f.flush()
            path = f.name

        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            os.unlink(path)


class TestApplyOverrides(unittest.TestCase):
    """Test applying configuration overrides to patterns."""

    def test_override_severity(self):
        config = ScannerConfig(
            pattern_overrides={"GOOGLE_API_KEY": {"severity": "low"}}
        )
        patterns = apply_overrides(DEFAULT_SECRET_PATTERNS, config)
        google_pattern = next(p for p in patterns if p.name == "GOOGLE_API_KEY")
        self.assertEqual(google_pattern.severity, "low")

    def test_add_custom_patterns(self):
        custom = SecretPattern(
            name="MY_COMPANY_KEY",
            regex=r"myco_[a-zA-Z0-9]{32}",
            severity="critical",
            description="My Company API Key",
        )
        config = ScannerConfig(custom_patterns=[custom])
        patterns = apply_overrides(DEFAULT_SECRET_PATTERNS, config)
        myco = next((p for p in patterns if p.name == "MY_COMPANY_KEY"), None)
        self.assertIsNotNone(myco)
        self.assertEqual(myco.severity, "critical")

    def test_default_patterns_preserved(self):
        config = ScannerConfig()
        patterns = apply_overrides(DEFAULT_SECRET_PATTERNS, config)
        self.assertGreaterEqual(len(patterns), len(DEFAULT_SECRET_PATTERNS))


if __name__ == "__main__":
    unittest.main()