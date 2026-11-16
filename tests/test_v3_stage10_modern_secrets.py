import tempfile
import time
import unittest
from pathlib import Path

from src.formatters import format_json
from src.patterns import DEFAULT_DETECTION_RULES
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


CASES = [
    ("CRT-SEC-063", "ONEPASSWORD_SECRET_KEY", assemble("A3", "-ABC123-", "ABCDEFGHIJK-ABCDE-ABCDE-ABCDE")),
    ("CRT-SEC-064", "ONEPASSWORD_SERVICE_ACCOUNT_TOKEN_JWT", assemble("ops", "_eyJ", "Ab3x" * 62, "Ab")),
    ("CRT-SEC-065", "AGE_SECRET_KEY", assemble("AGE", "-SECRET-KEY-1", ("QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" * 2)[:58])),
    ("CRT-SEC-066", "AIRTABLE_PERSONAL_ACCESS_TOKEN", assemble("pat", "Ab3xY7qP9LmN2Z.", "a1b2c3d4" * 8)),
    ("CRT-SEC-067", "CLICKHOUSE_CLOUD_API_SECRET", "4b1d" + "Ab3x" * 9 + "Y7"),
    ("CRT-SEC-068", "CLOJARS_API_TOKEN", assemble("CLO", "JARS_", "a1b2c3d4e5" * 6)),
    ("CRT-SEC-069", "CLOUDFLARE_ORIGIN_CA_KEY", assemble("v1", ".0-", "a1b2c3d4" * 3, "-", "a1b2c3d4" * 18, "a1")),
    ("CRT-SEC-070", "DUFFEL_API_TOKEN", assemble("duffel", "_test_", "Ab3x" * 10, "Y7q")),
    ("CRT-SEC-071", "DYNATRACE_API_TOKEN", assemble("dt0", "c01.", "Ab3x" * 6, ".", "a1B2c3D4" * 8)),
    ("CRT-SEC-072", "FRAMEIO_API_TOKEN", assemble("fio", "-u-", "Ab3x" * 16)),
    ("CRT-SEC-073", "GITLAB_CICD_JOB_TOKEN", assemble("gl", "cbt-", "A1b2C_z9Y8x7W6v5U4t3S2r1Q0")),
    ("CRT-SEC-074", "GITLAB_DEPLOY_TOKEN", assemble("gl", "dt-", "Ab3xY7qP9LmN2Z5v8K1s")),
    ("CRT-SEC-075", "GITLAB_RUNNER_AUTH_TOKEN", assemble("gl", "rt-", "Ab3xY7qP9LmN2Z5v8K1s")),
    ("CRT-SEC-076", "HEROKU_API_KEY_V2", assemble("HR", "KU-AA", "Ab3x" * 14, "Y7")),
    ("CRT-SEC-077", "INFRACOST_API_TOKEN", assemble("ic", "o-", "Ab3x" * 8)),
    ("CRT-SEC-078", "POSTMAN_API_TOKEN", assemble("PM", "AK-", "a1b2c3d4" * 3, "-", "a1b2c3d4" * 4, "a1")),
    ("CRT-SEC-079", "SENTRY_USER_AUTH_TOKEN", assemble("sntry", "u_", "a1b2c3d4" * 8)),
    ("CRT-SEC-080", "PLANETSCALE_API_TOKEN", assemble("pscale", "_tkn_", "Ab3x" * 8)),
]


class Stage10ModernSecretRulesTests(unittest.TestCase):
    @staticmethod
    def rules():
        return {rule.rule_id: rule for rule in DEFAULT_DETECTION_RULES}

    def test_registry_has_exact_stage10_ids_names_and_case_sensitive_flags(self):
        rules = self.rules()
        self.assertEqual(len(DEFAULT_DETECTION_RULES), 267)
        self.assertEqual(len(rules), 267)
        for rule_id, name, _ in CASES:
            with self.subTest(rule_id=rule_id):
                self.assertIn(rule_id, rules)
                self.assertEqual(rules[rule_id].name, name)
                self.assertEqual(rules[rule_id].flags, 0)
                self.assertEqual(rules[rule_id].severity, "critical")
                self.assertEqual(rules[rule_id].confidence, "high")

    def test_each_rule_matches_full_valid_token_but_not_short_or_swapped_prefix(self):
        rules = self.rules()
        for rule_id, _, value in CASES:
            with self.subTest(rule_id=rule_id):
                regex = rules[rule_id].compiled
                match = regex.search(value)
                self.assertIsNotNone(match)
                if match is None:
                    self.fail(f"{rule_id} did not match")
                self.assertEqual(match.group(0), value)
                self.assertIsNone(regex.search(value[:-1]))
                self.assertIsNone(regex.search(value.swapcase()))
                self.assertIsNone(regex.search("X" + value))
                extended = regex.search(value + "X")
                if extended is not None:
                    self.assertEqual(extended.group(0), value + "X")

    def test_directory_scan_finds_exactly_one_provider_finding_per_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "stage10.env").write_text("\n".join(value for _, _, value in CASES) + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([finding.rule_id for finding in result.findings], [case[0] for case in CASES])

    def test_variable_and_alternative_format_boundaries(self):
        rules = self.rules()
        ops = rules["CRT-SEC-064"].compiled
        long_ops = assemble("ops", "_eyJ", "Ab+/" * 525)
        match = ops.search(long_ops)
        self.assertIsNotNone(match)
        if match is None:
            self.fail("long 1Password service-account token did not match")
        self.assertEqual(match.group(0), long_ops)
        onepassword = rules["CRT-SEC-063"].compiled
        self.assertIsNotNone(onepassword.search(assemble("A3", "-ABC123-", "ABC123-ABCDE-ABCDE-ABCDE-ABCDE")))
        planetscale = rules["CRT-SEC-080"].compiled
        self.assertIsNotNone(planetscale.search(assemble("pscale", "_tkn_", "A" * 64)))
        self.assertIsNone(planetscale.search(assemble("pscale", "_tkn_", "A" * 65)))
        gitlab = rules["CRT-SEC-073"].compiled
        self.assertIsNotNone(gitlab.search(assemble("gl", "cbt-A_", "B" * 20)))
        self.assertIsNotNone(gitlab.search(assemble("gl", "cbt-Ab123_", "B" * 20)))
        self.assertIsNone(gitlab.search(assemble("gl", "cbt-Ab1234_", "B" * 20)))
        runner = rules["CRT-SEC-075"].compiled
        routable_min = assemble("gl", "rt-t1_", "A" * 27, ".bcdefghij")
        routable_max = assemble("gl", "rt-t9_", "A" * 300, ".12abcdefg")
        for routable in (routable_min, routable_max):
            match = runner.search(routable)
            self.assertIsNotNone(match)
            if match is None:
                self.fail("GitLab routable runner token did not match")
            self.assertEqual(match.group(0), routable)
        self.assertIsNone(runner.search(assemble("gl", "rt-t1_", "A" * 26, ".bcdefghij")))
        self.assertIsNone(runner.search(assemble("gl", "rt-t1_", "A" * 301, ".bcdefghij")))

    def test_unicode_identifier_embedding_is_not_a_token_boundary(self):
        rules = self.rules()
        for rule_id, _, value in CASES:
            with self.subTest(rule_id=rule_id):
                self.assertIsNone(rules[rule_id].compiled.search("é" + value))
                self.assertIsNone(rules[rule_id].compiled.search(value + "é"))

    def test_placeholders_are_suppressed_but_real_template_default_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "template.env")
            path.write_text("TOKEN=${TOKEN:-placeholder}\n", encoding="utf-8")
            self.assertEqual(SecretScanner(severity_threshold="low").scan_directory(tmp).findings, [])
            value = CASES[7][2]
            path.write_text(f"TOKEN=${{TOKEN:-{value}}}\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([finding.rule_id for finding in result.findings], ["CRT-SEC-070"])
            routable = assemble("gl", "rt-t1_", "A" * 27, ".bcdefghij")
            path.write_text(f"TOKEN=${{TOKEN:-{routable}}}\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([finding.rule_id for finding in result.findings], ["CRT-SEC-075"])

    def test_json_redacts_every_stage10_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "stage10.env").write_text("\n".join(value for _, _, value in CASES) + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            payload = format_json(result)
            for _, _, value in CASES:
                self.assertNotIn(value, payload)

    def test_long_nonmatching_line_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "long.txt").write_text("Z" * 200_000 + "\n", encoding="utf-8")
            start = time.monotonic()
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            elapsed = time.monotonic() - start
            self.assertEqual(result.findings, [])
            self.assertLess(elapsed, 2.0)

    def test_assignment_anchors_do_not_create_generic_duplicates(self):
        for anchor in ("token", "api_key", "secret", "password"):
            with self.subTest(anchor=anchor), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "assigned.env").write_text(
                    "\n".join(f"{anchor}={value}" for _, _, value in CASES) + "\n",
                    encoding="utf-8",
                )
                result = SecretScanner(severity_threshold="low").scan_directory(tmp)
                self.assertEqual([finding.rule_id for finding in result.findings], [case[0] for case in CASES])


if __name__ == "__main__":
    unittest.main()
