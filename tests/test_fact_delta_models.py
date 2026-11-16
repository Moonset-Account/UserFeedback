import json
import unittest

from src.observations import Observation, ObservationEvidence, ObservationLocation


class FactModelTests(unittest.TestCase):
    def observation(self, source_id="CRT-SEC-1", line=7):
        return Observation("native-line-rule", source_id, "secret", "secret", "high", "high",
                           ObservationLocation("src/app.py", line, line),
                           ObservationEvidence.from_raw("fixture-evidence"), {"rule_id": source_id})

    def test_fact_is_immutable_and_order_independent(self):
        from src.facts import Fact
        one, two = self.observation("CRT-SEC-1"), self.observation("CRT-SEC-2", 8)
        a = Fact.from_observations("FAM-SECRET", "secret", "src/app.py", [one, two], attributes={"provider": "x"})
        b = Fact.from_observations("FAM-SECRET", "secret", "src/app.py", [two, one], attributes={"provider": "x"})
        self.assertEqual(a.fact_id, b.fact_id)
        self.assertEqual(a.observation_ids, tuple(sorted(a.observation_ids)))
        self.assertEqual(a.to_dict(), json.loads(json.dumps(a.to_dict())))
        with self.assertRaises(AttributeError): a.family_id = "other"

    def test_conflicting_scope_and_bounds_rejected(self):
        from src.facts import Fact
        obs = self.observation()
        with self.assertRaises(ValueError): Fact.from_observations("", "secret", "scope", [obs])
        with self.assertRaises(ValueError): Fact.from_observations("FAM", "secret", "", [obs])
        with self.assertRaises(ValueError): Fact.from_observations("FAM", "secret", "scope", [])
        with self.assertRaises(ValueError): Fact("fact-sha256:" + "a" * 64, "FAM", "secret", "high", "high", (), "scope", {})

    def test_raw_evidence_is_not_in_fact(self):
        from src.facts import Fact
        fact = Fact.from_observations("FAM-SECRET", "secret", "src/app.py", [self.observation()])
        text = json.dumps(fact.to_dict())
        self.assertNotIn("fixture-evidence", text)
        self.assertNotIn("matched_text", text)


class DeltaModelTests(unittest.TestCase):
    def test_delta_kinds_and_deterministic_id(self):
        from src.facts import Delta
        added = Delta.create("added", (), ("fact-sha256:" + "a" * 64,), "src/app.py", "new observation")
        same = Delta.create("unchanged", ("fact-sha256:" + "a" * 64,), ("fact-sha256:" + "a" * 64,), "src/app.py", "same")
        changed = Delta.create("changed", ("fact-sha256:" + "a" * 64,), ("fact-sha256:" + "b" * 64,), "src/app.py", "scope changed")
        self.assertTrue(added.delta_id.startswith("delta-sha256:"))
        self.assertEqual(len(added.delta_id), len("delta-sha256:") + 64)
        self.assertEqual(same.kind, "unchanged")
        self.assertEqual(changed.kind, "changed")
        self.assertNotIn("SYNTHETIC", json.dumps(changed.to_dict()))

    def test_delta_validation(self):
        from src.facts import Delta
        fid = "fact-sha256:" + "a" * 64
        with self.assertRaises(ValueError): Delta.create("bad", (), (), "scope", "reason")
        with self.assertRaises(ValueError): Delta.create("added", (), (), "scope", "reason")
        with self.assertRaises(ValueError): Delta.create("unchanged", (), (fid,), "scope", "reason")
        with self.assertRaises(ValueError): Delta.create("changed", (fid,), (fid,), "scope", "reason")
        with self.assertRaises(ValueError): Delta.create("changed", (fid,), (), "scope", "")


if __name__ == "__main__":
    unittest.main()
