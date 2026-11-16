import unittest

from src.scanner import SecretScanner


class FigmaStableDetectorTests(unittest.TestCase):
    def test_detects_figma_pat_and_rejects_short_near_miss(self):
        token = "figd_" + "A" * 40
        diff = f"""--- a/config.py
+++ b/config.py
@@ -1,0 +1,2 @@
+FIGMA_TOKEN={token}
+FIGMA_TOKEN=figd_{{'A' * 20}}
"""
        result = SecretScanner(config_check=False).scan_diff_text(diff)
        ids = [finding.rule_id for finding in result.findings]
        self.assertEqual(ids.count("CRT-SEC-135"), 1)
    def test_provider_prefix_detectors(self):
        samples = {
            "CRT-SEC-136": "fc-" + "A" * 24,
            "CRT-SEC-137": "tr_prod_" + "A" * 24,
            "CRT-SEC-138": "tvly-" + "A" * 24,
            "CRT-SEC-139": "signkey-prod-" + "a" * 64,
            "CRT-SEC-141": "phx_" + "A" * 24,
            "CRT-SEC-142": "sntrys_" + "A" * 32,
            "CRT-SEC-143": "Authorization: aivenv1 " + "A" * 32,
            "CRT-SEC-144": "Authorization: FlyV1 " + "A" * 32,
            "CRT-SEC-145": "dt0s01." + "A" * 24 + "." + "A" * 64,
            "CRT-SEC-146": "bitwat_" + "A" * 32,
            "CRT-SEC-147": "ABSK" + "A" * 128,
            "CRT-SEC-148": "sk_car_" + "A" * 32,
            "CRT-SEC-149": "X-Hume-Api-Key: " + "A" * 32,
            "CRT-SEC-150": "rg_oidc_akr_" + "A" * 32,
            "CRT-SEC-151": "ops_" + "A" * 24 + "." + "B" * 16 + "." + "C" * 16,
            "CRT-SEC-152": "lsv2_pt_" + "A" * 32,
            "CRT-SEC-153": "cfat_" + "A" * 40 + "abcdef12",
            "CRT-SEC-154": "re_" + "A" * 32,
            "CRT-SEC-155": "fw_" + "A" * 32,
            "CRT-SEC-156": "fpk_" + "A" * 32,
            "CRT-SEC-157": "sb_secret_" + "A" * 32,
            "CRT-SEC-158": "whsec_" + "A" * 32,
            "CRT-SEC-159": "phs_" + "A" * 32,
            "CRT-SEC-160": "pha_" + "A" * 32,
            "CRT-SEC-161": "SSWS" + "A" * 32,
            "CRT-SEC-162": "dp.st." + "a" * 43,
            "CRT-SEC-163": "dp.ct." + "a" * 43,
            "CRT-SEC-164": "dp.sa." + "A" * 43,
            "CRT-SEC-165": "dp.scim." + "A" * 43,
            "CRT-SEC-166": "dp.audit." + "A" * 43,
            "CRT-SEC-167": "tskey-api-" + "A" * 12 + "-" + "B" * 24,
            "CRT-SEC-168": "tskey-scim-" + "A" * 12 + "-" + "B" * 24,
            "CRT-SEC-169": "tskey-client-" + "A" * 12 + "-" + "B" * 24,
            "CRT-SEC-170": "tskey-webhook-" + "A" * 12 + "-" + "B" * 24,
            "CRT-SEC-171": "hvr." + "A" * 32,
            "CRT-SEC-172": "hcaik_" + "a" * 32,
            "CRT-SEC-173": "ops_" + "A" * 64,
            "CRT-SEC-176": "sntrya_" + "A" * 64,
            "CRT-SEC-177": "hcxik_" + "a" * 32,
            "CRT-SEC-178": "dop_v1_" + "A" * 32,
            "CRT-SEC-179": "doo_v1_" + "A" * 32,
            "CRT-SEC-180": "pdl_live_apikey_" + "a1" * 13 + "_" + "Ab3x" * 5 + "Ab" + "_" + "Q7x",
            "CRT-SEC-181": "pdl_ntfset_" + "a1" * 13 + "_" + "Ab3x" * 8,
        }
        for expected, token in samples.items():
            with self.subTest(expected=expected):
                diff = f"--- a/config.py\n+++ b/config.py\n@@ -1,0 +1 @@\n+TOKEN={token}\n"
                result = SecretScanner(config_check=False).scan_diff_text(diff)
                self.assertIn(expected, [finding.rule_id for finding in result.findings])


if __name__ == "__main__":
    unittest.main()
