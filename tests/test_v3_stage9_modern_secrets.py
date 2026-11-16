import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.patterns import DEFAULT_DETECTION_RULES
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


CASES = [
    ("CRT-SEC-045", "GITHUB_FINE_GRAINED_PAT", assemble("github", "_pat_", "Ab3_" * 20, "Xy")),
    ("CRT-SEC-046", "SLACK_APP_TOKEN", assemble("xapp", "-1-A1B2C3D4-1234567890123-", "Ab3x" * 6)),
    ("CRT-SEC-047", "STRIPE_RESTRICTED_KEY", assemble("rk", "_live_", "Ab3x" * 6)),
    ("CRT-SEC-048", "PULUMI_ACCESS_TOKEN", assemble("pu", "l-", "a1b2" * 10)),
    ("CRT-SEC-049", "DOPPLER_PERSONAL_TOKEN", assemble("dp", ".pt.", "a1b2" * 10, "abc")),
    ("CRT-SEC-050", "REPLICATE_API_TOKEN", assemble("r", "8_", "Ab3x" * 9, "Q")),
    ("CRT-SEC-051", "GROQ_API_KEY", assemble("g", "sk_", "Ab3x" * 13)),
    ("CRT-SEC-052", "PERPLEXITY_API_KEY", assemble("ppl", "x-", "A1bZ" * 12)),
    ("CRT-SEC-053", "LANGSMITH_API_KEY_LEGACY", assemble("lsv2", "_pt_", "Ab3x" * 10, "_a1b2c3d4e5")),
    ("CRT-SEC-054", "PINECONE_API_KEY", assemble("pc", "sk_", "Ab3x" * 10)),
    ("CRT-SEC-055", "GRAFANA_SERVICE_ACCOUNT_TOKEN", assemble("gl", "sa_", "Ab3x" * 8, "_a1b2c3d4")),
    ("CRT-SEC-056", "SENTRY_ORG_AUTH_TOKEN", assemble("sntrys", "_eyJ", "Ab3x" * 10, "_", "Ab3x" * 10, "Ab3")),
    ("CRT-SEC-057", "DATABRICKS_PAT", assemble("da", "pi", "a1b2" * 8)),
    ("CRT-SEC-058", "TERRAFORM_CLOUD_TOKEN", assemble("A1b2C3d4E5f6G7.", "atlas", "v1.", "Ab3x" * 15)),
    ("CRT-SEC-059", "VAULT_SERVICE_TOKEN", assemble("hvs", ".", "Ab3x" * 22, "Qz")),
    ("CRT-SEC-060", "VAULT_BATCH_TOKEN", assemble("hvb", ".", "Ab3x" * 34, "Qz")),
    ("CRT-SEC-061", "NEW_RELIC_USER_KEY", assemble("NR", "AK-", "A1B2C3D4E5F6G7H8J9K0M2N3P4Q")),
    ("CRT-SEC-062", "CIRCLECI_PERSONAL_TOKEN", assemble("CCI", "PAT_", "Ab3x" * 10)),
]


class Stage9ModernSecretRulesTests(unittest.TestCase):
    def rules(self):
        return {rule.rule_id: rule for rule in DEFAULT_DETECTION_RULES}

    def test_exact_rule_set_and_metadata(self):
        rules = self.rules()
        expected_ids = {item[0] for item in CASES}
        self.assertEqual({rid for rid in rules if "CRT-SEC-045" <= rid <= "CRT-SEC-062"}, expected_ids)
        for rule_id, name, _ in CASES:
            rule = rules[rule_id]
            self.assertEqual(rule.name, name)
            self.assertEqual(rule.severity, "critical")
            self.assertEqual(rule.category, "secret")
            self.assertEqual(rule.confidence, "high")
            self.assertTrue(rule.remediation)

    def test_positive_boundary_and_short_near_miss_matrix(self):
        rules = self.rules()
        for rule_id, _, value in CASES:
            with self.subTest(rule_id=rule_id):
                regex = rules[rule_id].compiled
                match = regex.search(value)
                self.assertIsNotNone(match)
                if match is None:
                    self.fail(f"{rule_id} did not match its valid corpus value")
                self.assertEqual(match.group(0), value)
                self.assertIsNone(regex.search("X" + value))
                extended = regex.search(value + "X")
                if extended is not None:
                    self.assertEqual(extended.group(0), value + "X")
                self.assertIsNone(regex.search(value[:-1]))

    def test_stage9_token_formats_are_case_sensitive(self):
        rules = self.rules()
        for rule_id, _, value in CASES:
            with self.subTest(rule_id=rule_id):
                self.assertIsNone(rules[rule_id].compiled.search(value.swapcase()))

    def test_assignment_form_suppresses_generic_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "assigned.env").write_text(
                "\n".join(f"token={value}" for _, _, value in CASES) + "\n", encoding="utf-8"
            )
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([finding.rule_id for finding in result.findings], [item[0] for item in CASES])

    def test_complete_optional_databricks_suffix_is_in_match(self):
        rule = self.rules()["CRT-SEC-057"]
        value = CASES[12][2] + "-7"
        match = rule.compiled.search(value)
        self.assertIsNotNone(match)
        if match is not None:
            self.assertEqual(match.group(0), value)

    def test_directory_scan_finds_each_once_and_redacts_all_renderers(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "modern.env")
            source.write_text("\n".join(value for _, _, value in CASES) + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp, recursive=True)
            stage9 = [finding for finding in result.findings if finding.rule_id.startswith("CRT-SEC-") and 45 <= int(finding.rule_id[-3:]) <= 62]
            self.assertEqual([finding.rule_id for finding in stage9], [item[0] for item in CASES])
            self.assertEqual(len(stage9), 18)
            self.assertEqual(len(result.findings), 18)
            for rendering in (result.to_json(), result.to_markdown(), result.to_html(), result.to_sarif(), result.to_github()):
                for _, _, value in CASES:
                    self.assertNotIn(value, rendering)

    def test_placeholder_assignments_do_not_create_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = "\n".join(f"{name}=${{{name}}}" for _, name, _ in CASES) + "\n"
            content += "TOKEN=${TOKEN:-placeholder}\nAPI_KEY=${API_KEY:-replace_me}\n"
            Path(tmp, "template.env").write_text(content, encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual(result.findings, [])

    def test_actual_provider_secret_inside_template_default_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = CASES[0][2]
            Path(tmp, "real-default.env").write_text(f"TOKEN=${{TOKEN:-{value}}}\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([finding.rule_id for finding in result.findings], ["CRT-SEC-045"])

    def test_checksum_manifest_covers_complete_tracked_payload(self):
        root = Path(__file__).resolve().parents[1]
        manifest = root / "SHA256SUMS.txt"
        if not manifest.exists():
            self.skipTest("Source repository uses Git object integrity; no archive checksum manifest")
        tracked = set(subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines())
        tracked.discard("SHA256SUMS.txt")
        listed = {
            line.split("  ", 1)[1]
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        self.assertEqual(listed, tracked)

    def test_json_contains_only_redacted_evidence_for_stage9(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "modern.env").write_text(CASES[0][2] + "\n", encoding="utf-8")
            payload = json.loads(SecretScanner(severity_threshold="low").scan_directory(tmp).to_json())
            finding = next(item for item in payload["findings"] if item["rule_id"] == "CRT-SEC-045")
            self.assertEqual(finding["matched_text"], "[REDACTED]")
            self.assertEqual(finding["line_content"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
