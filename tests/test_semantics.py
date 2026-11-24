import unittest

from probe_planner import FAILED, MISSING, OK, Fault, Probe, evaluate_selection
from probe_planner.semantics import probe_outcome, signatures_for_probes


class SemanticsTests(unittest.TestCase):
    def test_site_outage_makes_every_probe_from_site_missing(self):
        probes = [
            Probe("beijing", "checkout", ("auth",), id="b-checkout"),
            Probe("beijing", "search", ("catalog",), id="b-search"),
            Probe("shanghai", "checkout", ("auth",), id="s-checkout"),
        ]
        fault = Fault.site_outage("beijing", id="site-down")

        self.assertEqual(probe_outcome(probes[0], fault), MISSING)
        self.assertEqual(probe_outcome(probes[1], fault), MISSING)
        self.assertEqual(probe_outcome(probes[2], fault), OK)

        signatures = signatures_for_probes(probes[:2], [fault])
        self.assertEqual(signatures["site-down"], (MISSING, MISSING))

    def test_dependency_failure_is_reported_as_failed_receipt(self):
        probe = Probe("beijing", "checkout", ("edge", "auth", "db"), id="p")
        auth_down = Fault.dependency_failure("auth", id="auth-down")
        catalog_down = Fault.dependency_failure("catalog", id="catalog-down")

        self.assertEqual(probe_outcome(probe, auth_down), FAILED)
        self.assertEqual(probe_outcome(probe, catalog_down), OK)

    def test_missing_receipt_takes_precedence_over_path_failure(self):
        probe = Probe("beijing", "checkout", ("auth",), id="p")
        weird_single_fault = Fault(
            "both-effects",
            unavailable_sites=frozenset({"beijing"}),
            failed_resources=frozenset({"auth"}),
        )
        self.assertEqual(probe_outcome(probe, weird_single_fault), MISSING)

    def test_target_entry_itself_is_a_failable_resource(self):
        probe = Probe("site-a", "checkout", ("edge",), id="p")
        checkout_down = Fault.entry_failure("checkout", id="checkout-down")
        self.assertEqual(probe_outcome(probe, checkout_down), FAILED)

    def test_evaluate_selection_reports_equal_signature_pairs(self):
        probes = [
            {"id": "p1", "site": "a", "entry": "e1", "dependencies": ["d1"]},
            {"id": "p2", "site": "b", "entry": "e2", "dependencies": ["d2"]},
        ]
        faults = ["d1", "d2", "unseen"]
        report = evaluate_selection(probes, faults, ["p1"])

        self.assertEqual(report["signatures"]["d1"], (FAILED,))
        self.assertEqual(report["signatures"]["d2"], (OK,))
        self.assertEqual(report["signatures"]["unseen"], (OK,))
        self.assertIn(("d2", "unseen"), report["ambiguous_pairs"])
        self.assertEqual(report["distinguished_pair_count"], 2)
        self.assertEqual(report["ambiguous_pair_count"], 1)


if __name__ == "__main__":
    unittest.main()
