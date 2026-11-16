import unittest

from src.patterns import DEFAULT_DETECTION_RULES, match_secret


class CloudinaryDetectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rule = next(r for r in DEFAULT_DETECTION_RULES if r.rule_id == "CRT-SEC-182")

    @staticmethod
    def credential():
        return "cloudinary://123456789012345:abcdefghijklmnopqrstuvwxyzA@demo-cloud"

    def test_official_credential_url_positive(self):
        matches = match_secret(self.credential(), [self.rule])
        self.assertEqual([rule.rule_id for rule, _ in matches], ["CRT-SEC-182"])

    def test_assignment_positive(self):
        matches = match_secret("CLOUDINARY_URL=" + self.credential(), [self.rule])
        self.assertEqual([rule.rule_id for rule, _ in matches], ["CRT-SEC-182"])

    def test_wrong_scheme_negative(self):
        self.assertFalse(match_secret(self.credential().replace("cloudinary://", "https://"), [self.rule]))

    def test_wrong_api_key_length_negative(self):
        self.assertFalse(match_secret(self.credential().replace("123456789012345", "1234"), [self.rule]))

    def test_wrong_secret_length_negative(self):
        self.assertFalse(match_secret(self.credential().replace("abcdefghijklmnopqrstuvwxyzA", "short"), [self.rule]))

    def test_missing_cloud_name_negative(self):
        self.assertFalse(match_secret(self.credential().rsplit("@", 1)[0] + "@", [self.rule]))

    def test_placeholder_negative(self):
        self.assertFalse(match_secret("cloudinary://<api_key>:<api_secret>@<cloud_name>", [self.rule]))


if __name__ == "__main__":
    unittest.main()
