import json
import unittest


class IncidentModelTests(unittest.TestCase):
    def ids(self):
        return "fact-sha256:" + "a" * 64, "delta-sha256:" + "b" * 64

    def test_incident_is_immutable_and_deterministic(self):
        from src.incidents import Incident
        fact, delta = self.ids()
        one = Incident.create("open", "repo:src/app.py", [fact], [delta], "high", "high", "redacted summary")
        two = Incident.create("open", "repo:src/app.py", [fact], [delta], "high", "high", "redacted summary")
        self.assertEqual(one.incident_id, two.incident_id)
        self.assertRegex(one.incident_id, r"^incident-sha256:[0-9a-f]{64}$")
        self.assertEqual(one.to_dict(), json.loads(json.dumps(one.to_dict())))
        with self.assertRaises(AttributeError): one.status = "resolved"

    def test_order_independence_and_validation(self):
        from src.incidents import Incident
        fact_a = "fact-sha256:" + "a" * 64; fact_b = "fact-sha256:" + "b" * 64
        delta = "delta-sha256:" + "c" * 64
        one = Incident.create("open", "scope", [fact_a, fact_b], [delta], "high", "high", "summary")
        two = Incident.create("open", "scope", [fact_b, fact_a], [delta], "high", "high", "summary")
        self.assertEqual(one.incident_id, two.incident_id)
        for args in [
            ("bad", "scope", [fact_a], [delta], "high", "high", "summary"),
            ("open", "", [fact_a], [delta], "high", "high", "summary"),
            ("open", "scope", [], [delta], "high", "high", "summary"),
            ("open", "scope", [fact_a], [delta], "urgent", "high", "summary"),
            ("open", "scope", [fact_a], [delta], "high", "certain", "summary"),
            ("open", "scope", [fact_a], [delta], "high", "high", "raw matched_text marker"),
        ]:
            with self.subTest(args=args):
                with self.assertRaises(ValueError): Incident.create(*args)

    def test_closed_status_transitions(self):
        from src.incidents import Incident
        self.assertEqual(Incident.allowed_transitions("open"), ("resolved", "superseded"))
        self.assertTrue(Incident.can_transition("open", "resolved"))
        self.assertTrue(Incident.can_transition("open", "superseded"))
        self.assertFalse(Incident.can_transition("resolved", "open"))
        self.assertFalse(Incident.can_transition("superseded", "resolved"))

    def test_summary_redaction(self):
        from src.incidents import Incident
        fact, delta = self.ids()
        incident = Incident.create("open", "scope", [fact], [delta], "medium", "low", "[REDACTED]")
        self.assertNotIn("matched_text", json.dumps(incident.to_dict()))
        self.assertNotIn("fixture-evidence", json.dumps(incident.to_dict()))


if __name__ == "__main__":
    unittest.main()
