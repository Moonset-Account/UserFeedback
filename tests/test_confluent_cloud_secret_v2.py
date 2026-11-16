import base64
import unittest
import zlib

from src.patterns import DEFAULT_DETECTION_RULES, match_secret, match_rules_all


class ConfluentCloudApiSecretV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rule = next(r for r in DEFAULT_DETECTION_RULES if r.rule_id == "CRT-SEC-184")

    @staticmethod
    def token():
        payload = ("Ab3+/xY9" * 7)[:54]
        checksum = base64.b64encode(zlib.crc32(payload.encode("ascii")).to_bytes(4, "little"))[:6].decode("ascii")
        return "cflt" + payload + checksum

    def test_official_shape_positive(self):
        token = self.token()
        self.assertEqual(len(token), 64)
        self.assertEqual([r.rule_id for r, _ in match_secret(token, [self.rule])], ["CRT-SEC-184"])
        self.assertEqual([r.rule_id for r, _ in match_rules_all(token, "fixture.txt", [self.rule])], ["CRT-SEC-184"])

    def test_bad_checksum_negative(self):
        token = self.token()
        self.assertFalse(match_secret(token[:-1] + ("A" if token[-1] != "A" else "B"), [self.rule]))

    def test_wrong_prefix_negative(self):
        self.assertFalse(match_secret("xflt" + self.token()[4:], [self.rule]))

    def test_wrong_length_negative(self):
        self.assertFalse(match_secret(self.token()[:-1], [self.rule]))
        self.assertFalse(match_secret(self.token() + "A", [self.rule]))

    def test_invalid_alphabet_negative(self):
        self.assertFalse(match_secret(self.token()[:10] + "!" + self.token()[11:], [self.rule]))

    def test_embedded_boundary_negative(self):
        self.assertFalse(match_secret("x" + self.token(), [self.rule]))


if __name__ == "__main__":
    unittest.main()
