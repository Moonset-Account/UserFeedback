import unittest

from src.patterns import DEFAULT_DETECTION_RULES, match_secret


class AzureDevOpsPersonalAccessTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rule = next(r for r in DEFAULT_DETECTION_RULES if r.rule_id == "CRT-SEC-183")

    @staticmethod
    def token():
        return "abcdefghijklmnopqrstuvwxyz012345679ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnoAZDOabcd"

    def test_microsoft_documented_shape_positive(self):
        token = self.token()
        self.assertEqual(len(token), 84)
        self.assertEqual(token[76:80], "AZDO")
        self.assertEqual([r.rule_id for r, _ in match_secret(token, [self.rule])], ["CRT-SEC-183"])

    def test_assignment_positive(self):
        self.assertEqual([r.rule_id for r, _ in match_secret("AZURE_DEVOPS_EXT_PAT=" + self.token(), [self.rule])], ["CRT-SEC-183"])

    def test_wrong_marker_negative(self):
        self.assertFalse(match_secret(self.token().replace("AZDO", "AZDX"), [self.rule]))

    def test_marker_at_wrong_position_negative(self):
        self.assertFalse(match_secret("AZDO" + "A" * 76 + "abcd", [self.rule]))

    def test_too_short_negative(self):
        self.assertFalse(match_secret(self.token()[:-1], [self.rule]))

    def test_too_long_negative(self):
        self.assertFalse(match_secret(self.token() + "A", [self.rule]))

    def test_invalid_alphabet_negative(self):
        self.assertFalse(match_secret(self.token().replace("a", "-", 1), [self.rule]))

    def test_embedded_in_larger_identifier_negative(self):
        self.assertFalse(match_secret("x" + self.token(), [self.rule]))

    def test_placeholder_negative(self):
        self.assertFalse(match_secret("<" + "A" * 75 + "AZDO" + "A" * 4 + ">", [self.rule]))


if __name__ == "__main__":
    unittest.main()
