from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.scanner import SecretScanner


class ExplicitPlaceholderSuppressionTests(unittest.TestCase):
    def test_explicit_placeholder_values_are_suppressed_in_diff_mode(self) -> None:
        cases = (
            ("API_KEY", "REDACTED_GOOGLE_API_KEY"),
            ("TOKEN", "MASKED_GITHUB_TOKEN_VALUE"),
            ("SECRET_KEY", "PLACEHOLDER_STRIPE_SECRET"),
        )
        body = "".join(f"+{key}: \"{value}\"\n" for key, value in cases)
        diff = (
            "diff --git a/config/service.yaml b/config/service.yaml\n"
            "--- a/config/service.yaml\n"
            "+++ b/config/service.yaml\n"
            f"@@ -0,0 +1,{len(cases)} @@\n{body}"
        )
        result = SecretScanner(config_check=False).scan_diff_text(diff)
        self.assertEqual([], result.findings)

    def test_explicit_placeholder_values_are_suppressed_in_directory_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config.env").write_text(
                "API_KEY=REDACTED_GOOGLE_API_KEY\n"
                "TOKEN=MASKED_GITHUB_TOKEN_VALUE\n"
                "SECRET_KEY=PLACEHOLDER_STRIPE_SECRET\n",
                encoding="utf-8",
            )
            result = SecretScanner(config_check=False).scan_directory(str(root))
        self.assertEqual([], result.findings)

    def test_real_like_generic_values_and_noncanonical_redacted_prefix_are_detected(self) -> None:
        diff = (
            "diff --git a/config/service.env b/config/service.env\n"
            "--- a/config/service.env\n"
            "+++ b/config/service.env\n"
            "@@ -0,0 +1,4 @@\n"
            "+TOKEN=real_production_token_value_123456\n"
            "+API_KEY=prefix_REDACTED_but_still_value\n"
            "+SECRET_KEY=notPLACEHOLDER_actual_value\n"
            "+PASSWORD=REDACTEDBUTREAL_PASSWORD_123\n"
        )
        result = SecretScanner(config_check=False).scan_diff_text(diff)
        self.assertEqual(
            {"TOKEN_LITERAL", "API_KEY_LITERAL", "SECRET_LITERAL", "PASSWORD_LITERAL"},
            {finding.pattern_name for finding in result.findings},
        )


if __name__ == "__main__":
    unittest.main()
