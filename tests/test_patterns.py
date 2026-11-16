"""Unit tests for secret/config detection patterns."""

import unittest
from src.patterns import (
    SecretPattern, ConfigCategory, DEFAULT_SECRET_PATTERNS, DEFAULT_CONFIG_CATEGORIES,
    match_secret, classify_config_file, get_secret_patterns, get_config_categories,
    SECURITY_PATH_KEYWORDS,
)
from tests.synthetic_values import assemble


class TestDefaultSecretPatterns(unittest.TestCase):
    """Test that default secret patterns are defined correctly."""

    def test_default_patterns_exist(self):
        patterns = get_secret_patterns()
        self.assertGreaterEqual(len(patterns), 20, "Should have at least 20 secret patterns")

    def test_pattern_has_required_fields(self):
        for p in DEFAULT_SECRET_PATTERNS:
            self.assertTrue(p.name, "Pattern must have a name")
            self.assertTrue(p.regex, "Pattern must have a regex")
            self.assertIn(p.severity, ("critical", "high", "medium", "low"),
                         f"Pattern {p.name} has invalid severity: {p.severity}")
            self.assertTrue(p.description, "Pattern must have a description")

    def test_pattern_compiles(self):
        for p in DEFAULT_SECRET_PATTERNS:
            try:
                p.compiled
            except re.error as e:
                self.fail(f"Pattern {p.name} has invalid regex: {e}")


class TestAWSAccessKeyDetection(unittest.TestCase):
    """Test AWS Access Key detection."""

    def test_detects_aws_access_key(self):
        matches = match_secret('AWS_ACCESS_KEY_ID = "' + assemble("AK", "IA", "IOSFODNN7EXAMPLE") + '"')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0].name, "AWS_ACCESS_KEY")

    def test_no_false_positive_short(self):
        matches = match_secret('key = "AKIA"')
        self.assertEqual(len(matches), 0)


class TestAWSSecretKeyDetection(unittest.TestCase):
    """Test AWS Secret Key detection."""

    def test_detects_aws_secret_key(self):
        matches = match_secret('AWS_SECRET_ACCESS_KEY="' + assemble("wJalrXUt", "nFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY") + '"')
        self.assertGreaterEqual(len(matches), 1)
        self.assertEqual(matches[0][0].name, "AWS_SECRET_KEY")

    def test_detects_aws_secret_key_spaces(self):
        matches = match_secret('aws_secret = ' + assemble("wJalrXUt", "nFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"))
        self.assertGreaterEqual(len(matches), 1)


class TestGitHubTokenDetection(unittest.TestCase):
    """Test GitHub token detection."""

    def test_detects_github_pat(self):
        matches = match_secret(assemble("gh", "p_", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij1234"))
        self.assertGreaterEqual(len(matches), 1)
        found_names = [m[0].name for m in matches]
        self.assertIn("GITHUB_TOKEN", found_names)

    def test_detects_github_oauth_token(self):
        matches = match_secret('github_oauth_token = "' + assemble("abcdef1234567890", "abcdef1234567890abcdef12") + '"')
        self.assertGreaterEqual(len(matches), 1)


class TestSlackTokenDetection(unittest.TestCase):
    """Test Slack token detection."""

    def test_detects_slack_bot_token(self):
        matches = match_secret(assemble("xo", "xb-", "1234567890-1234567890123-abcdefghijklmnopqrstuvwx"))
        self.assertGreaterEqual(len(matches), 1)
        self.assertEqual(matches[0][0].name, "SLACK_TOKEN")


class TestStripeKeyDetection(unittest.TestCase):
    """Test Stripe key detection."""

    def test_detects_stripe_secret_key(self):
        matches = match_secret('STRIPE_SECRET_KEY=' + assemble("sk", "_live_", "abcdefghijklmnopqrstuvwxyz012345"))
        self.assertGreaterEqual(len(matches), 1)
        found_names = [m[0].name for m in matches]
        self.assertIn("STRIPE_KEY", found_names)

    def test_detects_stripe_publishable_key(self):
        matches = match_secret('STRIPE_KEY=' + assemble("pk", "_live_", "abcdefghijklmnopqrstuvwxyz012345678"))
        self.assertGreaterEqual(len(matches), 1)


class TestDatabaseURLDetection(unittest.TestCase):
    """Test database URL detection."""

    def test_detects_postgres_url(self):
        matches = match_secret('DATABASE_URL = "postgresql://user:pass@localhost/mydb"')
        self.assertGreaterEqual(len(matches), 1)
        found_names = [m[0].name for m in matches]
        self.assertIn("DATABASE_URL", found_names)

    def test_detects_mysql_url(self):
        matches = match_secret('DB = "mysql://root:password@localhost/test"')
        self.assertGreaterEqual(len(matches), 1)


class TestPrivateKeyDetection(unittest.TestCase):
    """Test private key detection."""

    def test_detects_rsa_private_key(self):
        matches = match_secret(assemble('-----BEGIN ', 'RSA PRIVATE KEY-----'))
        self.assertGreaterEqual(len(matches), 1)
        found_names = [m[0].name for m in matches]
        self.assertIn("PRIVATE_KEY", found_names)

    def test_detects_ec_private_key(self):
        matches = match_secret(assemble('-----BEGIN ', 'EC PRIVATE KEY-----'))
        self.assertGreaterEqual(len(matches), 1)


class TestPasswordLiteralDetection(unittest.TestCase):
    """Test password literal detection."""

    def test_detects_password_assignment(self):
        matches = match_secret('password = "mysecretpassword123"')
        self.assertGreaterEqual(len(matches), 1)
        found_names = [m[0].name for m in matches]
        self.assertIn("PASSWORD_LITERAL", found_names)

    def test_detects_password_colon(self):
        matches = match_secret('password: mysecretpassword123')
        self.assertGreaterEqual(len(matches), 1)


class TestAPIKeyLiteralDetection(unittest.TestCase):
    """Test API key literal detection."""

    def test_detects_api_key_assignment(self):
        matches = match_secret('api_key = "' + assemble("sk", "_live_", "abcdef1234567890abcdef") + '"')
        self.assertGreaterEqual(len(matches), 1)

    def test_detects_apikey_assignment(self):
        matches = match_secret('apikey=abcdef1234567890abcdef1234')
        self.assertGreaterEqual(len(matches), 1)


class TestConfigCategoryClassification(unittest.TestCase):
    """Test config file classification."""

    def test_classifies_env_file(self):
        cats = classify_config_file(".env.production")
        cat_names = [c.name for c in cats]
        self.assertIn("ENV_CONFIG", cat_names)

    def test_classifies_github_actions(self):
        cats = classify_config_file(".github/workflows/deploy.yml")
        cat_names = [c.name for c in cats]
        self.assertIn("CI_CONFIG", cat_names)

    def test_classifies_docker_compose(self):
        cats = classify_config_file("docker-compose.yml")
        cat_names = [c.name for c in cats]
        self.assertIn("INFRA_CONFIG", cat_names)

    def test_classifies_auth_path(self):
        cats = classify_config_file("src/auth/login.py")
        cat_names = [c.name for c in cats]
        self.assertIn("SECURITY_CONFIG", cat_names)

    def test_no_classification_for_regular_file(self):
        cats = classify_config_file("src/utils/helpers.py")
        self.assertEqual(len(cats), 0)

    def test_classifies_config_json(self):
        cats = classify_config_file("config.json")
        cat_names = [c.name for c in cats]
        self.assertIn("AUTH_CONFIG", cat_names)


class TestSeverityValues(unittest.TestCase):
    """Test that all patterns have valid severity values."""

    def test_all_patterns_valid_severity(self):
        valid = {"critical", "high", "medium", "low"}
        for p in DEFAULT_SECRET_PATTERNS:
            self.assertIn(p.severity, valid, f"{p.name} has invalid severity: {p.severity}")

    def test_critical_patterns(self):
        critical = [p for p in DEFAULT_SECRET_PATTERNS if p.severity == "critical"]
        self.assertGreaterEqual(len(critical), 3, "Should have at least 3 critical patterns")

    def test_high_patterns(self):
        high = [p for p in DEFAULT_SECRET_PATTERNS if p.severity == "high"]
        self.assertGreaterEqual(len(high), 3, "Should have at least 3 high patterns")


if __name__ == "__main__":
    unittest.main()
