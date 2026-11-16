"""Stage 12 acceptance tests: 18 source-backed secret detection rules (CRT-SEC-099..116)."""
import json
import tempfile
import time
import unittest

from src.patterns import DEFAULT_DETECTION_RULES
from tests.synthetic_values import assemble


class TestV3Stage12SourceBackedSecrets(unittest.TestCase):
    """18 cases must be detected by their dedicated rules."""

    @classmethod
    def rules(cls):
        return {r.name: r for r in DEFAULT_DETECTION_RULES}

    # ------------------------------------------------------------------
    # Minimal valid corpus — one primary match per rule
    # ------------------------------------------------------------------
    CASES = {
        "ATLASSIAN_TOKEN": (
            assemble("ATA", "TT3", "A" * 186),
            "CRT-SEC-099",
        ),
        "NPM_ACCESS_TOKEN": (
            assemble("npm", "_", "a" * 36),
            "CRT-SEC-100",
        ),
        "GITLAB_FEATURE_FLAG_TOKEN": (
            assemble("gl", "ffct-", "A" * 20),
            "CRT-SEC-101",
        ),
        "DIGITALOCEAN_OAUTH": (
            assemble("doo", "_v1_", "a" * 64),
            "CRT-SEC-102",
        ),
        "EASYPOST_API_KEY": (
            assemble("EZ", "AK", "a" * 54),
            "CRT-SEC-103",
        ),
        "EASYPOST_TEST_KEY": (
            assemble("EZ", "TK", "a" * 54),
            "CRT-SEC-104",
        ),
        "GOCARDLESS_TOKEN": (
            assemble("li", "ve_", "A" * 40),
            "CRT-SEC-105",
        ),
        "LOB_API_KEY": (
            assemble("li", "ve_", "a" * 35),
            "CRT-SEC-106",
        ),
        "NEWRELIC_INSERT_KEY": (
            assemble("NR", "II-", "a" * 32),
            "CRT-SEC-107",
        ),
        "NEWRELIC_BROWSER_TOKEN": (
            assemble("NR", "JS-", "a" * 19),
            "CRT-SEC-108",
        ),
        "DEFINED_NETWORKING_TOKEN": (
            assemble("dn", "key-", "a" * 26, "-", "b" * 52),
            "CRT-SEC-109",
        ),
        "SONAR_TOKEN": (
            assemble("sq", "u_", "a" * 40),
            "CRT-SEC-110",
        ),
        "TYPEFORM_TOKEN": (
            assemble("tf", "p_", "a" * 59),
            "CRT-SEC-111",
        ),
        "META_ACCESS_TOKEN": (
            assemble("EA", "AC", "a" * 100),
            "CRT-SEC-112",
        ),
        "ALIBABA_ACCESS_KEY": (
            assemble("LT", "AI", "a" * 20),
            "CRT-SEC-113",
        ),
        "ARTIFACTORY_KEY": (
            assemble("AK", "Cp", "A" * 69),
            "CRT-SEC-114",
        ),
        "NOTION_API_TOKEN": (
            assemble("nt", "n_", "1" * 11, "A" * 35),
            "CRT-SEC-115",
        ),
        "FLUTTERWAVE_SECRET_KEY": (
            assemble("FLW", "SECK_TEST-", "a" * 32, "-X"),
            "CRT-SEC-116",
        ),
    }

    def test_all_18_cases_detected(self):
        rules = self.rules()
        for name, (token, expected_id) in self.CASES.items():
            with self.subTest(name=name):
                rule = rules.get(name)
                self.assertIsNotNone(rule, f"Rule {name} not found in registry")
                m = rule.compiled.search(token)
                self.assertIsNotNone(m, f"Rule {name} did not match token: {token[:50]}...")
                self.assertEqual(rule.rule_id, expected_id, f"Rule {name} has wrong ID")

    def test_scanner_returns_exactly_one_dedicated_id_per_case(self):
        from pathlib import Path
        from src.scanner import SecretScanner
        for name, (token, expected_id) in self.CASES.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "secret.env").write_text("token=" + token + "\n", encoding="utf-8")
                result = SecretScanner(severity_threshold="low").scan_directory(tmp)
                self.assertEqual([finding.rule_id for finding in result.findings], [expected_id])

    # ------------------------------------------------------------------
    # Boundary tests: min, max, one-short, one-long
    # ------------------------------------------------------------------
    def test_boundary_atlassian(self):
        rule = self.rules()["ATLASSIAN_TOKEN"]
        # ATATT3 + 186 chars minimum (upstream exact)
        self.assertIsNotNone(rule.compiled.search(assemble("ATA", "TT3", "A" * 186)))
        self.assertIsNone(rule.compiled.search(assemble("ATA", "TT3", "A" * 185)))
        self.assertIsNone(rule.compiled.search(assemble("ATA", "TT3", "A" * 187)))

    def test_boundary_npm(self):
        rule = self.rules()["NPM_ACCESS_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("npm", "_", "a" * 36)))
        self.assertIsNone(rule.compiled.search(assemble("npm", "_", "a" * 35)))
        self.assertIsNone(rule.compiled.search(assemble("npm", "_", "a" * 37)))

    def test_boundary_notion(self):
        rule = self.rules()["NOTION_API_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("nt", "n_", "1" * 11, "A" * 35)))
        self.assertIsNone(rule.compiled.search(assemble("nt", "n_", "1" * 10, "A" * 35)))
        self.assertIsNone(rule.compiled.search(assemble("nt", "n_", "1" * 11, "A" * 36)))

    def test_boundary_gitlab_feature_flag(self):
        rule = self.rules()["GITLAB_FEATURE_FLAG_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("gl", "ffct-", "A" * 20)))
        self.assertIsNone(rule.compiled.search(assemble("gl", "ffct-", "A" * 19)))
        self.assertIsNone(rule.compiled.search(assemble("gl", "ffct-", "A" * 21)))

    def test_defined_networking_exact_segments(self):
        rule = self.rules()["DEFINED_NETWORKING_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("dn", "key-", "a" * 26, "-", "b" * 52)))
        self.assertIsNone(rule.compiled.search(assemble("dn", "key-", "a" * 25, "-", "b" * 52)))
        self.assertIsNone(rule.compiled.search(assemble("dn", "key-", "a" * 26, "-", "b" * 51)))

    def test_boundary_digitalocean_oauth(self):
        rule = self.rules()["DIGITALOCEAN_OAUTH"]
        self.assertIsNotNone(rule.compiled.search(assemble("doo", "_v1_", "a" * 64)))
        self.assertIsNone(rule.compiled.search(assemble("doo", "_v1_", "a" * 63)))

    def test_boundary_easypost(self):
        rule = self.rules()["EASYPOST_API_KEY"]
        self.assertIsNotNone(rule.compiled.search(assemble("EZ", "AK", "a" * 54)))
        self.assertIsNone(rule.compiled.search(assemble("EZ", "AK", "a" * 53)))

    def test_boundary_easypost_test(self):
        rule = self.rules()["EASYPOST_TEST_KEY"]
        self.assertIsNotNone(rule.compiled.search(assemble("EZ", "TK", "a" * 54)))
        self.assertIsNone(rule.compiled.search(assemble("EZ", "TK", "a" * 53)))

    def test_boundary_meta(self):
        rule = self.rules()["META_ACCESS_TOKEN"]
        # EAAC + 100+ alnum
        self.assertIsNotNone(rule.compiled.search(assemble("EA", "AC", "a" * 100)))
        self.assertIsNotNone(rule.compiled.search(assemble("EA", "AC", "a" * 200)))
        # One short (99 chars after EAAC)
        self.assertIsNone(rule.compiled.search(assemble("EA", "AC", "a" * 99)))

    def test_boundary_alibaba(self):
        rule = self.rules()["ALIBABA_ACCESS_KEY"]
        self.assertIsNotNone(rule.compiled.search(assemble("LT", "AI", "a" * 20)))
        self.assertIsNone(rule.compiled.search(assemble("LT", "AI", "a" * 19)))

    def test_boundary_artifactory(self):
        rule = self.rules()["ARTIFACTORY_KEY"]
        self.assertIsNotNone(rule.compiled.search(assemble("AK", "Cp", "A" * 69)))
        self.assertIsNone(rule.compiled.search(assemble("AK", "Cp", "A" * 68)))

    def test_boundary_flutterwave(self):
        rule = self.rules()["FLUTTERWAVE_SECRET_KEY"]
        self.assertIsNotNone(rule.compiled.search(assemble("FLW", "SECK_TEST-", "a" * 32, "-X")))
        # One short body
        self.assertIsNone(rule.compiled.search(assemble("FLW", "SECK_TEST-", "a" * 31, "-X")))
        # Upstream alphabet is case-insensitive a-h plus digits; I-Z are invalid.
        self.assertIsNone(rule.compiled.search(assemble("FLW", "SECK_TEST-", "Z" * 32, "-X")))

    def test_boundary_gocardless(self):
        rule = self.rules()["GOCARDLESS_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("li", "ve_", "A" * 40)))
        mixed = "a-b_c=1" * 5
        self.assertEqual(len(mixed), 35)
        token = assemble("li", "ve_", mixed, "x" * 5)
        match = rule.compiled.search(token)
        self.assertIsNotNone(match)
        self.assertEqual(match.span(), (0, len(token)))

    def test_boundary_lob(self):
        rule = self.rules()["LOB_API_KEY"]
        self.assertIsNotNone(rule.compiled.search(assemble("li", "ve_", "a" * 35)))
        self.assertIsNotNone(rule.compiled.search("test_" + "a" * 35))
        self.assertIsNone(rule.compiled.search(assemble("li", "ve_", "a" * 34)))

    def test_boundary_sonar(self):
        rule = self.rules()["SONAR_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("sq", "u_", "a" * 40)))
        self.assertIsNotNone(rule.compiled.search("sqp_" + "a" * 40))
        self.assertIsNotNone(rule.compiled.search("sqa_" + "a" * 40))

    def test_boundary_typeform(self):
        rule = self.rules()["TYPEFORM_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("tf", "p_", "a" * 59)))
        # One short
        self.assertIsNone(rule.compiled.search(assemble("tf", "p_", "a" * 58)))

    # ------------------------------------------------------------------
    # Case sensitivity tests
    # ------------------------------------------------------------------
    def test_case_sensitive_prefixes(self):
        """Stage 12 rules use flags=0 (case-sensitive)."""
        rules = self.rules()
        # Atlassian ATATT3 is case-sensitive
        self.assertIsNotNone(rules["ATLASSIAN_TOKEN"].compiled.search(assemble("ATA", "TT3", "A" * 186)))
        self.assertIsNone(rules["ATLASSIAN_TOKEN"].compiled.search("atatt3" + "A" * 186))

        # EasyPost EZAK/EZTK are case-sensitive
        self.assertIsNotNone(rules["EASYPOST_API_KEY"].compiled.search(assemble("EZ", "AK", "a" * 54)))
        self.assertIsNone(rules["EASYPOST_API_KEY"].compiled.search("ezak" + "a" * 54))

        # EasyPost prefixes are explicitly case-sensitive upstream.

    # ------------------------------------------------------------------
    # Registry invariant
    # ------------------------------------------------------------------
    def test_registry_count(self):
        from src.patterns import DEFAULT_DETECTION_RULES
        self.assertEqual(len(DEFAULT_DETECTION_RULES), 267)

    def test_provider_alphabet_suffixes_are_not_truncated(self):
        rules = self.rules()
        hostile = {
            "ATLASSIAN_TOKEN": assemble("ATA", "TT3", "A" * 187),
            "NPM_ACCESS_TOKEN": assemble("npm", "_", "a" * 37),
            "GITLAB_FEATURE_FLAG_TOKEN": assemble("gl", "ffct-", "A" * 21),
            "DIGITALOCEAN_OAUTH": assemble("doo", "_v1_", "a" * 65),
            "EASYPOST_API_KEY": assemble("EZ", "AK", "A" * 55),
            "EASYPOST_TEST_KEY": assemble("EZ", "TK", "A" * 55),
            "GOCARDLESS_TOKEN": assemble("li", "ve_", "A" * 40, "="),
            "LOB_API_KEY": assemble("li", "ve_", "a" * 36),
            "NEWRELIC_INSERT_KEY": assemble("NR", "II-", "A" * 32, "-"),
            "NEWRELIC_BROWSER_TOKEN": assemble("NR", "JS-", "a" * 20),
            "DEFINED_NETWORKING_TOKEN": assemble("dn", "key-", "a" * 27, "-", "b" * 52),
            "SONAR_TOKEN": assemble("sq", "u_", "A" * 40, "-"),
            "TYPEFORM_TOKEN": assemble("tf", "p_", "A" * 59, "."),
            "ALIBABA_ACCESS_KEY": assemble("LT", "AI", "A" * 21),
            "ARTIFACTORY_KEY": assemble("AK", "Cp", "A" * 70),
            "NOTION_API_TOKEN": assemble("nt", "n_", "1" * 11, "A" * 36),
        }
        for name, value in hostile.items():
            with self.subTest(name=name):
                self.assertIsNone(rules[name].compiled.search(value))

    def test_ascii_and_unicode_word_embedding_rejected(self):
        rules = self.rules()
        values = {
            "NPM_ACCESS_TOKEN": assemble("npm", "_", "a" * 36),
            "GITLAB_FEATURE_FLAG_TOKEN": assemble("gl", "ffct-", "A" * 20),
            "EASYPOST_API_KEY": assemble("EZ", "AK", "A" * 54),
            "META_ACCESS_TOKEN": assemble("EA", "AC", "A" * 100),
            "ARTIFACTORY_KEY": assemble("AK", "Cp", "A" * 69),
        }
        for name, value in values.items():
            for wrapped in ("x" + value, "_" + value, "ż" + value, value + "ż"):
                with self.subTest(name=name, boundary=wrapped[:1]):
                    self.assertIsNone(rules[name].compiled.search(wrapped))

    def test_real_shell_default_detected_and_placeholder_default_suppressed(self):
        from pathlib import Path
        from src.scanner import SecretScanner
        with tempfile.TemporaryDirectory() as tmp:
            real = assemble("npm", "_", "a" * 36)
            Path(tmp, "config.env").write_text(
                "NPM_TOKEN=${NPM_TOKEN:-" + real + "}\nOTHER=${OTHER:-placeholder}\n",
                encoding="utf-8",
            )
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            ids = [finding.rule_id for finding in result.findings]
            self.assertEqual(ids.count("CRT-SEC-100"), 1)

    def test_json_redacts_raw_stage12_token(self):
        from pathlib import Path
        from src.formatters import format_json
        from src.scanner import SecretScanner
        with tempfile.TemporaryDirectory() as tmp:
            raw = assemble("AK", "Cp", "A" * 69)
            Path(tmp, "secret.env").write_text(raw + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            rendered = format_json(result)
            self.assertIn("CRT-SEC-114", rendered)
            self.assertNotIn(raw, rendered)
            json.loads(rendered)

    def test_hostile_line_performance_is_bounded(self):
        rules = self.rules()
        hostile = "x" * 200_000 + assemble(" ATA", "TT3", "A" * 250, "=")
        started = time.monotonic()
        for name in self.CASES:
            rules[name].compiled.search(hostile)
        self.assertLess(time.monotonic() - started, 2.0)

    # ------------------------------------------------------------------
    # Placeholder defaults should NOT create findings
    # ------------------------------------------------------------------
    def test_placeholder_defaults_suppressed(self):
        """Synthetic defaults like ${TOKEN:-placeholder} should be suppressed."""
        from src.allowlist import is_suppressed
        rules = []  # no allowlist rules needed for placeholder check
        self.assertTrue(is_suppressed("ATLASSIAN_TOKEN", "critical", "test.env", "${ATLASSIAN_TOKEN:-placeholder}", rules))
        self.assertTrue(is_suppressed("NPM_ACCESS_TOKEN", "critical", "test.env", "${NPM_TOKEN:-default}", rules))
        self.assertTrue(is_suppressed("DEFINED_NETWORKING_TOKEN", "critical", "test.env", "${DNKEY:-example}", rules))

    def test_upstream_case_insensitive_variants(self):
        rules = self.rules()
        variants = {
            "NPM_ACCESS_TOKEN": "NPM_" + "a" * 36,
            "GOCARDLESS_TOKEN": "LIVE_" + "A" * 40,
            "LOB_API_KEY": "LIVE_" + "a" * 35,
            "NEWRELIC_INSERT_KEY": "nrii-" + "a" * 32,
            "NEWRELIC_BROWSER_TOKEN": "nrjs-" + "a" * 19,
            "SONAR_TOKEN": "SQU_" + "a" * 40,
            "TYPEFORM_TOKEN": "TFP_" + "a" * 59,
        }
        for name, value in variants.items():
            with self.subTest(name=name):
                match = rules[name].compiled.search(value)
                self.assertIsNotNone(match)
                self.assertEqual(match.span(), (0, len(value)))


if __name__ == "__main__":
    unittest.main()
