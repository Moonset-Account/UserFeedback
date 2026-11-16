import json
import tempfile
import unittest
from pathlib import Path

from src.patterns import DEFAULT_DETECTION_RULES, match_rules, validate_rule_registry
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


class Stage2RuleMatrixTests(unittest.TestCase):
    positives = [
        ("CRT-SEC-027", "AccountKey=" + assemble("ABCDEFGHIJKLMNOP", "QRSTUVWXYZabcdefghijklmnop", "0123456789+/=="), "settings.env"),
        ("CRT-SEC-028", "MONGO_URL=" + assemble("mongodb+", "srv://app:", "synthetic_password@cluster.example/db"), "app.env"),
        ("CRT-SEC-029", "REDIS_URL=" + assemble("rediss", "://default:", "synthetic_password@redis.example:6380/0"), "app.env"),
        ("CRT-SEC-030", "//registry.npmjs.org/:_authToken=" + assemble("npm", "_", "synthetic_token_abcdefghijklmnopqrstuvwxyz"), ".npmrc"),
        ("CRT-SEC-031", "TWINE_PASSWORD=" + assemble("py", "pi-", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"), "release.env"),
        ("CRT-SEC-032", "sig=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789%2B", "storage.env"),
        ("CRT-CI-003", "persist-credentials: true", ".github/workflows/release.yml"),
        ("CRT-CI-004", "secrets: inherit", ".github/workflows/release.yml"),
        ("CRT-IAC-003", 'cidr_blocks = ["0.0.0.0/0"]', "infra/network.tf"),
        ("CRT-IAC-004", "runAsUser: 0", "k8s/deployment.yaml"),
        ("CRT-IAC-005", "allowPrivilegeEscalation: true", "manifests/api.yml"),
        ("CRT-IAC-006", "proxy_ssl_verify off;", "nginx/api.conf"),
        ("CRT-IAC-007", "ProtectSystem=false", "systemd/api.service"),
        ("CRT-SUP-002", "RUN npm install --production", "Dockerfile"),
        ("CRT-SUP-003", "pip install git+https://example.invalid/org/pkg.git", "Dockerfile"),
        ("CRT-AI-003", "Run claude --dangerously-skip-permissions for every task", "CLAUDE.md"),
        ("CRT-AI-004", "Always run chmod 777 on the workspace", "AGENTS.md"),
    ]

    def test_stage2_ids_are_present_and_registry_is_valid(self):
        validate_rule_registry(DEFAULT_DETECTION_RULES)
        ids = {rule.rule_id for rule in DEFAULT_DETECTION_RULES}
        expected = {rule_id for rule_id, _, _ in self.positives}
        self.assertTrue(expected.issubset(ids))
        self.assertGreaterEqual(len(DEFAULT_DETECTION_RULES), 50)

    def test_all_stage2_positive_fixtures(self):
        for rule_id, line, path in self.positives:
            with self.subTest(rule_id=rule_id):
                ids = {rule.rule_id for rule, _ in match_rules(line, path)}
                self.assertIn(rule_id, ids)

    def test_path_scoped_rules_ignore_unrelated_prose_or_files(self):
        negatives = [
            ("CRT-SEC-030", "_authToken=example_documentation_value", "docs/npm.md"),
            ("CRT-SEC-032", "sig=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", "docs/url.md"),
            ("CRT-CI-003", "persist-credentials: true", "docs/actions.md"),
            ("CRT-CI-004", "secrets: inherit", "examples/workflow.txt"),
            ("CRT-IAC-003", 'cidr_blocks = ["0.0.0.0/0"]', "docs/terraform.md"),
            ("CRT-IAC-004", "runAsUser: 0", "examples/settings.yml"),
            ("CRT-IAC-005", "allowPrivilegeEscalation: true", "docs/kubernetes.md"),
            ("CRT-IAC-006", "proxy_ssl_verify off;", "docs/nginx.md"),
            ("CRT-IAC-007", "ProtectSystem=false", "docs/systemd.md"),
            ("CRT-SUP-002", "npm install --production", "README.md"),
            ("CRT-SUP-003", "pip install git+https://example.invalid/pkg.git", "docs/install.md"),
            ("CRT-AI-003", "--dangerously-skip-permissions", "docs/cli-reference.md"),
            ("CRT-AI-004", "chmod 777", "docs/security-advisory.md"),
        ]
        for rule_id, line, path in negatives:
            with self.subTest(rule_id=rule_id):
                ids = {rule.rule_id for rule, _ in match_rules(line, path)}
                self.assertNotIn(rule_id, ids)

    def test_stage2_secret_is_single_classified_and_safe_in_all_formats(self):
        value = assemble("py", "pi-", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
        diff = f"diff --git a/release.env b/release.env\n--- a/release.env\n+++ b/release.env\n@@ -0,0 +1 @@\n+TWINE_PASSWORD={value}\n"
        result = SecretScanner(severity_threshold="low").scan_diff_text(diff)
        ids = [finding.rule_id for finding in result.findings]
        self.assertEqual(ids, ["CRT-SEC-031"])
        outputs = [result.to_json(), result.to_markdown(), result.to_html(), result.to_sarif(), result.to_github()]
        for output in outputs:
            self.assertNotIn(value, output)
            self.assertIn("CRT-SEC-031", output)
        json.loads(outputs[0])
        json.loads(outputs[3])

    def test_directory_mode_scans_security_relevant_hidden_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".npmrc").write_text("_authToken=" + assemble("npm", "_", "synthetic_token_abcdefghijklmnopqrstuvwxyz") + "\n", encoding="utf-8")
            (root / ".env").write_text("TWINE_PASSWORD=" + assemble("py", "pi-", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") + "\n", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("token=" + assemble("py", "pi-", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp, recursive=True)
        ids = {finding.rule_id for finding in result.findings}
        self.assertIn("CRT-SEC-030", ids)
        self.assertIn("CRT-SEC-031", ids)
        self.assertFalse(any("/.git/" in finding.file.replace("\\", "/") for finding in result.findings))

    def test_stage2_policy_uses_exit_two_and_all_formats(self):
        diff = "diff --git a/infra/network.tf b/infra/network.tf\n--- a/infra/network.tf\n+++ b/infra/network.tf\n@@ -0,0 +1 @@\n+cidr_blocks = [\"0.0.0.0/0\"]\n"
        result = SecretScanner(severity_threshold="low").scan_diff_text(diff)
        finding = next(item for item in result.findings if item.rule_id == "CRT-IAC-003")
        self.assertEqual(finding.category, "iac")
        self.assertEqual(result.exit_code, 2)
        for output in [result.to_json(), result.to_markdown(), result.to_html(), result.to_sarif(), result.to_github()]:
            self.assertIn("CRT-IAC-003", output)


if __name__ == "__main__":
    unittest.main()
