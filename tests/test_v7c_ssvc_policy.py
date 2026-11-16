"""RED tests for explicit, non-overclaiming SSVC normalization."""

import unittest

from src.vulnerability.sources.ssvc import normalize_ssvc_content


class TestV7cSsvcPolicy(unittest.TestCase):
    def test_normalizes_camel_case_and_marks_full_decision_not_evaluable(self):
        result = normalize_ssvc_content({"exploitation": "Active Exploitation", "automatable": "Yes", "technicalImpact": "Total"})
        self.assertEqual(result["exploitation"], "active")
        self.assertEqual(result["technical_impact"], "total")
        self.assertEqual(result["decision"], "not_evaluable")
        self.assertTrue(result["follow_up_signal"])
        self.assertIn("deployment", result["missing_decision_points"])

    def test_rejects_unknown_decision_point_values(self):
        for field, value in (("exploitation", "unknown"), ("automatable", "maybe"), ("technical_impact", "critical")):
            content = {"exploitation": "none", "automatable": "no", "technical_impact": "partial"}
            content[field] = value
            with self.assertRaises(ValueError):
                normalize_ssvc_content(content)

    def test_requires_all_three_vulnrichment_decision_points(self):
        with self.assertRaises(ValueError):
            normalize_ssvc_content({"exploitation": "none", "automatable": "no"})


if __name__ == "__main__":
    unittest.main()
