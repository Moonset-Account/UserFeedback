import unittest

from src.scanner import SecretScanner


class SpanAwareMatchTests(unittest.TestCase):
    def test_provider_and_independent_password_both_survive(self):
        diff = """--- a/config.py
+++ b/config.py
@@ -1,0 +1 @@
+OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA password=independent-secret
"""
        result = SecretScanner(config_check=False).scan_diff_text(diff)
        ids = [finding.rule_id for finding in result.findings]
        self.assertIn("CRT-SEC-021", ids)
        self.assertIn("CRT-SEC-014", ids)

    def test_two_independent_values_for_same_rule_are_reported(self):
        diff = """--- a/config.py
+++ b/config.py
@@ -1,0 +1 @@
+password=alpha-secret password=bravo-secret
"""
        result = SecretScanner(config_check=False).scan_diff_text(diff)
        passwords = [finding for finding in result.findings if finding.rule_id == "CRT-SEC-014"]
        self.assertEqual(len(passwords), 2)


if __name__ == "__main__":
    unittest.main()
