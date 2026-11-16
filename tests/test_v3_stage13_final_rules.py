"""Stage 13: final 18 source-backed rule acceptance tests (CRT-SEC-117..134)."""
import tempfile
import time
import unittest
from pathlib import Path

from src.patterns import DEFAULT_DETECTION_RULES
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


class Stage13RulesTests(unittest.TestCase):
    CASES = {
        "AUTHRESS_SERVICE_KEY": (assemble("sc", "_abcde.abcd.acc_key-", "abcdef.abcdEFGHijklMNOPqrstUVWXyz0123456789+/="), "CRT-SEC-117"),
        "BEDROCK_SHORT_LIVED_KEY": (assemble("bedrock", "-api-key-", "YmVkcm9jay5hbWF6b25hd3MuY29t"), "CRT-SEC-118"),
        "GITLAB_FEED_TOKEN": (assemble("gl", "ft-", "A" * 20), "CRT-SEC-119"),
        "GITLAB_INCOMING_MAIL_TOKEN": (assemble("gl", "imt-", "A" * 25), "CRT-SEC-120"),
        "GITLAB_AGENT_TOKEN": (assemble("gl", "agent-", "A" * 50), "CRT-SEC-121"),
        "GITLAB_OAUTH_SECRET": (assemble("gl", "oas-", "A" * 64), "CRT-SEC-122"),
        "GITLAB_PIPELINE_TRIGGER_TOKEN": (assemble("gl", "ptt-", "a" * 40), "CRT-SEC-123"),
        "GITLAB_RRT": (assemble("GR", "1348941", "A" * 20), "CRT-SEC-124"),
        "GITLAB_SCIM_TOKEN": (assemble("gl", "soat-", "A" * 20), "CRT-SEC-125"),
        "GITLAB_SESSION_COOKIE": (assemble("_gitlab", "_session=", "a" * 32), "CRT-SEC-126"),
        "SHOPIFY_CUSTOM_TOKEN": (assemble("sh", "pca_", "a" * 32), "CRT-SEC-127"),
        "SHOPIFY_PRIVATE_TOKEN": (assemble("sh", "ppa_", "a" * 32), "CRT-SEC-128"),
        "SHOPIFY_SHARED_SECRET": (assemble("sh", "pss_", "a" * 32), "CRT-SEC-129"),
        "INTRA42_CLIENT_SECRET": (assemble("s-s", "4t2ud-", "a" * 64), "CRT-SEC-130"),
        "SLACK_LEGACY_WORKSPACE_TOKEN": (assemble("xo", "xa-", "A" * 8), "CRT-SEC-131"),
        "SLACK_WEBHOOK_URL": (assemble("https://hooks.slack.com/", "services/", "A" * 43), "CRT-SEC-132"),
        "SLACK_CONFIG_ACCESS_TOKEN": (assemble("xoxe.", "xoxb-1-", "A" * 166), "CRT-SEC-133"),
        "SLACK_CONFIG_REFRESH_TOKEN": (assemble("xoxe", "-1-", "A" * 146), "CRT-SEC-134"),
    }

    @staticmethod
    def rules():
        return {rule.name: rule for rule in DEFAULT_DETECTION_RULES}

    def test_registry_and_metadata(self):
        self.assertEqual(len(DEFAULT_DETECTION_RULES), 267)
        rules = self.rules()
        for name, (_, rule_id) in self.CASES.items():
            with self.subTest(name=name):
                self.assertEqual(rules[name].rule_id, rule_id)
                self.assertTrue(rules[name].unicode_boundaries)

    def test_all_canonical_tokens_match_full_span(self):
        rules = self.rules()
        for name, (token, _) in self.CASES.items():
            with self.subTest(name=name):
                match = rules[name].compiled.search(token)
                self.assertIsNotNone(match)
                self.assertEqual(match.span(), (0, len(token)))

    def test_scanner_produces_exactly_one_expected_id(self):
        for name, (token, rule_id) in self.CASES.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "secret.env").write_text("token=" + token + "\n", encoding="utf-8")
                result = SecretScanner(severity_threshold="low").scan_directory(tmp)
                self.assertEqual([finding.rule_id for finding in result.findings], [rule_id])

    def test_fixed_length_rules_reject_short_long_and_suffix_truncation(self):
        rules = self.rules()
        cases = {
            "GITLAB_FEED_TOKEN": (assemble("gl", "ft-"), 20, "A"),
            "GITLAB_INCOMING_MAIL_TOKEN": (assemble("gl", "imt-"), 25, "A"),
            "GITLAB_AGENT_TOKEN": (assemble("gl", "agent-"), 50, "A"),
            "GITLAB_OAUTH_SECRET": (assemble("gl", "oas-"), 64, "A"),
            "GITLAB_PIPELINE_TRIGGER_TOKEN": (assemble("gl", "ptt-"), 40, "a"),
            "GITLAB_RRT": ("GR1348941", 20, "A"),
            "GITLAB_SCIM_TOKEN": (assemble("gl", "soat-"), 20, "A"),
            "SHOPIFY_CUSTOM_TOKEN": (assemble("sh", "pca_"), 32, "a"),
            "SHOPIFY_PRIVATE_TOKEN": (assemble("sh", "ppa_"), 32, "a"),
            "SHOPIFY_SHARED_SECRET": (assemble("sh", "pss_"), 32, "a"),
        }
        for name, (prefix, size, char) in cases.items():
            rule = rules[name]
            with self.subTest(name=name):
                self.assertIsNone(rule.compiled.search(prefix + char * (size - 1)))
                self.assertIsNone(rule.compiled.search(prefix + char * (size + 1)))

    def test_slack_legacy_workspace_min_max_and_overlong(self):
        rule = self.rules()["SLACK_LEGACY_WORKSPACE_TOKEN"]
        self.assertIsNotNone(rule.compiled.search(assemble("xo", "xa-", "A" * 8)))
        self.assertIsNotNone(rule.compiled.search(assemble("xo", "xr-1-", "A" * 48)))
        self.assertIsNone(rule.compiled.search(assemble("xo", "xa-", "A" * 7)))
        self.assertIsNone(rule.compiled.search(assemble("xo", "xa-", "A" * 49)))
        self.assertIsNone(rule.compiled.search("XOXA-" + "A" * 8))

    def test_slack_webhook_min_max_and_overlong(self):
        rule = self.rules()["SLACK_WEBHOOK_URL"]
        for size in (43, 56):
            self.assertIsNotNone(rule.compiled.search(assemble("https://hooks.slack.com/", "services/", "A" * size)))
        self.assertIsNone(rule.compiled.search(assemble("https://hooks.slack.com/", "services/", "A" * 42)))
        self.assertIsNone(rule.compiled.search(assemble("https://hooks.slack.com/", "services/", "A" * 57)))

    def test_unicode_and_ascii_embedding_rejected(self):
        rules = self.rules()
        for name in ("GITLAB_FEED_TOKEN", "SHOPIFY_CUSTOM_TOKEN", "SLACK_CONFIG_REFRESH_TOKEN"):
            token = self.CASES[name][0]
            for wrapped in ("x" + token, "_" + token, "ż" + token, token + "ż"):
                with self.subTest(name=name):
                    self.assertIsNone(rules[name].compiled.search(wrapped))

    def test_hostile_line_bounded(self):
        rules = self.rules()
        line = "x" * 200_000
        started = time.monotonic()
        for name in self.CASES:
            rules[name].compiled.search(line)
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
