import tempfile
import time
import unittest
from pathlib import Path

from src.baseline import write_baseline
from src.formatters import format_json
from src.patterns import DEFAULT_DETECTION_RULES
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


CASES = [
    ("CRT-SEC-081", "ADOBE_CLIENT_SECRET", assemble("p8", "e-", "Ab3x" * 8)),
    ("CRT-SEC-082", "FLYIO_ACCESS_TOKEN", assemble("fo", "1_", "Ab3x" * 10, "Y7q")),
    ("CRT-SEC-083", "GRAFANA_CLOUD_API_TOKEN", assemble("gl", "c_", "Ab3x" * 8)),
    ("CRT-SEC-084", "HARNESS_API_KEY", assemble("pat", ".", "Ab3xY7qP9LmN2Z5v8K1sQ0", ".", "Ab3x" * 6, ".", "Ab3x" * 5)),
    ("CRT-SEC-085", "HUGGINGFACE_ORG_TOKEN", assemble("api", "_org_", "AbCd" * 8, "Ef")),
    ("CRT-SEC-086", "OCTOPUS_DEPLOY_API_KEY", assemble("AP", "I-", "AB3XY7QP9LMN2Z5V8K1S4D6F0C")),
    ("CRT-SEC-087", "OPENSHIFT_USER_TOKEN", assemble("sha", "256~", "Ab3x" * 10, "Y7q")),
    ("CRT-SEC-088", "PREFECT_API_TOKEN", assemble("pn", "u_", "Ab3x" * 9)),
    ("CRT-SEC-089", "README_API_TOKEN", assemble("rd", "me_", "a1b2c3d4e5" * 7)),
    ("CRT-SEC-090", "RUBYGEMS_API_TOKEN", assemble("ruby", "gems_", "a1b2c3d4" * 6)),
    ("CRT-SEC-091", "SCALINGO_API_TOKEN", assemble("tk", "-us-", "Ab3x" * 12)),
    ("CRT-SEC-092", "BREVO_API_TOKEN", assemble("xkey", "sib-", "a1b2c3d4" * 8, "-", "Ab3x" * 4)),
    ("CRT-SEC-093", "SHIPPO_API_TOKEN", assemble("shippo", "_live_", "a1b2c3d4" * 5)),
    ("CRT-SEC-094", "SOURCEGRAPH_ACCESS_TOKEN", assemble("sg", "p_", "a1b2c3d4" * 5)),
    ("CRT-SEC-095", "SQUARE_ACCESS_TOKEN", assemble("sq", "0atp-", "Ab3x" * 5, "Y7")),
    ("CRT-SEC-096", "MAXMIND_LICENSE_KEY", "Ab3xY7_" + "Ab3x" * 7 + "Q" + "_mmk"),
    ("CRT-SEC-097", "PLANETSCALE_OAUTH_TOKEN", assemble("pscale", "_oauth_", "Ab3x" * 8)),
    ("CRT-SEC-098", "SETTLEMINT_SERVICE_TOKEN", assemble("sm", "_sat_", "Ab3x" * 4)),
]


class Stage11SourceBackedRulesTests(unittest.TestCase):
    @staticmethod
    def rules():
        return {rule.rule_id: rule for rule in DEFAULT_DETECTION_RULES}

    def test_registry_has_exact_stage11_metadata(self):
        rules = self.rules()
        self.assertEqual(len(DEFAULT_DETECTION_RULES), 267)
        self.assertEqual(len(rules), 267)
        for rule_id, name, _ in CASES:
            with self.subTest(rule_id=rule_id):
                rule = rules[rule_id]
                self.assertEqual(rule.name, name)
                self.assertEqual(rule.flags, 0)
                self.assertTrue(rule.unicode_boundaries)
                self.assertEqual((rule.severity, rule.confidence), ("critical", "high"))

    def test_full_span_short_case_and_embedding_boundaries(self):
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
                for prefix in ("X", "_", "é"):
                    self.assertIsNone(regex.search(prefix + value))
                for suffix in ("X", "_", "é"):
                    extended = regex.search(value + suffix)
                    if extended is not None:
                        self.assertEqual(extended.group(0), value + suffix)

    def test_directory_scan_returns_one_provider_finding_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "stage11.env").write_text("\n".join(v for _, _, v in CASES) + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([f.rule_id for f in result.findings], [c[0] for c in CASES])

    def test_provider_alternatives_and_bounded_ranges(self):
        rules = self.rules()
        fly = rules["CRT-SEC-082"].compiled
        for value in (
            assemble("fm", "1a_", "A" * 100),
            assemble("fm", "1r_", "A" * 100, "==="),
            assemble("fm", "2_", "A" * 100),
        ):
            match = fly.search(value)
            self.assertIsNotNone(match)
            if match is None:
                self.fail("Fly.io alternative did not match")
            self.assertEqual(match.group(0), value)
        self.assertIsNone(fly.search(assemble("fm", "2_", "A" * 99)))

        grafana = rules["CRT-SEC-083"].compiled
        self.assertIsNotNone(grafana.search(assemble("gl", "c_", "A" * 400)))
        self.assertIsNone(grafana.search(assemble("gl", "c_", "A" * 401)))
        self.assertIsNotNone(rules["CRT-SEC-084"].compiled.search("sat." + "A" * 22 + "." + "B" * 24 + "." + "C" * 20))

        sourcegraph = rules["CRT-SEC-094"].compiled
        self.assertIsNotNone(sourcegraph.search(assemble("sg", "p_", "a" * 16, "_", "b" * 40)))
        self.assertIsNotNone(sourcegraph.search(assemble("sg", "p_local_", "b" * 40)))
        self.assertIsNone(sourcegraph.search("a" * 40))

        square = rules["CRT-SEC-095"].compiled
        self.assertIsNotNone(square.search("EAAA" + "A" * 22))
        self.assertIsNotNone(square.search(assemble("sq", "0atp-", "A" * 60)))
        self.assertIsNone(square.search(assemble("sq", "0atp-", "A" * 21)))
        self.assertIsNone(square.search(assemble("sq", "0atp-", "A" * 61)))

        planetscale = rules["CRT-SEC-097"].compiled
        self.assertIsNotNone(planetscale.search(assemble("pscale", "_oauth_", "A" * 64)))
        self.assertIsNotNone(planetscale.search(assemble("pscale", "_oauth_", "A" * 32, "==")))
        self.assertIsNotNone(planetscale.search(assemble("pscale", "_oauth_", "A" * 32, "/Ab3x" * 6, "==")))
        self.assertIsNone(planetscale.search(assemble("pscale", "_oauth_", "A" * 65)))
        # dot and hyphen are delimiters, not in base64 charset — token matches, delimiter does not
        m = planetscale.search(assemble("pscale", "_oauth_", "A" * 32, "."))
        self.assertIsNotNone(m)
        self.assertFalse(m.group(0).endswith("."))
        m = planetscale.search(assemble("pscale", "_oauth_", "A" * 32, "-"))
        self.assertIsNotNone(m)
        self.assertFalse(m.group(0).endswith("-"))

        planetscale_tkn = rules["CRT-SEC-080"].compiled
        self.assertIsNotNone(planetscale_tkn.search(assemble("pscale", "_tkn_", "A" * 64)))
        self.assertIsNotNone(planetscale_tkn.search(assemble("pscale", "_tkn_", "A" * 32, "==")))
        self.assertIsNotNone(planetscale_tkn.search(assemble("pscale", "_tkn_", "A" * 32, "/Ab3x" * 6, "==")))
        self.assertIsNone(planetscale_tkn.search(assemble("pscale", "_tkn_", "A" * 65)))
        m = planetscale_tkn.search(assemble("pscale", "_tkn_", "A" * 32, "."))
        self.assertIsNotNone(m)
        self.assertFalse(m.group(0).endswith("."))
        m = planetscale_tkn.search(assemble("pscale", "_tkn_", "A" * 32, "-"))
        self.assertIsNotNone(m)
        self.assertFalse(m.group(0).endswith("-"))
        self.assertIsNotNone(rules["CRT-SEC-093"].compiled.search(assemble("shippo", "_test_", "a" * 40)))

    def test_provider_alphabet_suffixes_cannot_be_truncated(self):
        rules = self.rules()
        # Suffix chars that ARE in the body charset — entire string must NOT match
        invalid_full = {
            "CRT-SEC-080": (
                assemble("pscale", "_tkn_", "A" * 64, "===="),
            ),
            "CRT-SEC-082": (
                assemble("fm", "1a_", "A" * 100, "===="),
                assemble("fo", "1_", "A" * 43, "-"),
            ),
            "CRT-SEC-083": (
                assemble("gl", "c_", "A" * 32, "===="),
                assemble("gl", "c_", "A" * 400, "/"),
            ),
            "CRT-SEC-084": ("pat." + "A" * 22 + "." + "B" * 24 + "." + "C" * 20 + "-",),
            "CRT-SEC-087": ("sha256~" + "A" * 43 + "-",),
            "CRT-SEC-091": ("tk-us-" + "A" * 48 + "-",),
            "CRT-SEC-097": (
                assemble("pscale", "_oauth_", "A" * 64, "===="),
            ),
        }
        for rule_id, values in invalid_full.items():
            for value in values:
                with self.subTest(rule_id=rule_id, kind="full_reject", suffix=value[-1]):
                    self.assertIsNone(rules[rule_id].compiled.search(value))

        # Suffix chars NOT in body charset — token matches, suffix is NOT absorbed
        invalid_partial = {
            "CRT-SEC-095": (assemble("sq", "0atp-", "A" * 60, "-suffix"),),
        }
        for rule_id, values in invalid_partial.items():
            for value in values:
                with self.subTest(rule_id=rule_id, kind="no_absorb"):
                    m = rules[rule_id].compiled.search(value)
                    self.assertIsNotNone(m)
                    self.assertFalse(m.group(0).endswith("-"))

    def test_placeholder_defaults_and_json_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "template.env")
            path.write_text("TOKEN=${TOKEN:-placeholder}\n", encoding="utf-8")
            self.assertEqual(SecretScanner(severity_threshold="low").scan_directory(tmp).findings, [])
            path.write_text("\n".join(f"TOKEN=${{TOKEN:-{value}}}" for _, _, value in CASES) + "\n", encoding="utf-8")
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual([f.rule_id for f in result.findings], [c[0] for c in CASES])
            payload = format_json(result)
            for _, _, value in CASES:
                self.assertNotIn(value, payload)

    def test_long_nonmatching_line_remains_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "long.txt").write_text("Z" * 200_000 + "\n", encoding="utf-8")
            start = time.monotonic()
            result = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual(result.findings, [])
            self.assertLess(time.monotonic() - start, 2.0)

    def test_exact_allowlist_and_baseline_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "tokens.env"
            target.write_text("\n".join(value for _, _, value in CASES) + "\n", encoding="utf-8")
            first = SecretScanner(severity_threshold="low").scan_directory(tmp)
            self.assertEqual(len(first.findings), 18)

            allowlist = root / ".secretsallowlist"
            allowlist.write_text(f"value:{CASES[0][2]}\n", encoding="utf-8")
            allowed = SecretScanner(allowlist_path=str(allowlist), severity_threshold="low").scan_directory(tmp)
            self.assertEqual(len(allowed.findings), 17)
            self.assertNotIn("CRT-SEC-081", [finding.rule_id for finding in allowed.findings])
            allowlist.unlink()

            baseline = root / "baseline.json"
            write_baseline(str(baseline), [finding.fingerprint for finding in first.findings])
            baselined = SecretScanner(baseline_path=str(baseline), severity_threshold="low").scan_directory(tmp)
            self.assertEqual(baselined.findings, [])
            self.assertEqual(baselined.baseline_suppressed, 18)

    def test_assignment_anchors_have_no_generic_duplicates(self):
        for anchor in ("password", "api_key", "token", "secret"):
            with self.subTest(anchor=anchor), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "assigned.env").write_text("\n".join(f"{anchor}={v}" for _, _, v in CASES) + "\n", encoding="utf-8")
                result = SecretScanner(severity_threshold="low").scan_directory(tmp)
                self.assertEqual([f.rule_id for f in result.findings], [c[0] for c in CASES])


if __name__ == "__main__":
    unittest.main()
