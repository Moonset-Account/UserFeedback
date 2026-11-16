import json
import unittest
from pathlib import Path


class ObservationModelTests(unittest.TestCase):
    def make_location(self, **overrides):
        from src.observations import ObservationLocation
        values = dict(path="src/app.py", start_line=7, end_line=7, identity_path="src/app.py", hunk_id="h1")
        values.update(overrides)
        return ObservationLocation(**values)

    def make_evidence(self, raw="fixture-evidence-marker"):
        from src.observations import ObservationEvidence
        return ObservationEvidence.from_raw(raw, redacted="[REDACTED]")

    def make_observation(self, **overrides):
        from src.observations import Observation
        values = dict(source_kind="native-line-rule", source_id="CRT-SEC-999", kind="secret",
                      category="secret", severity="high", confidence="high",
                      location=self.make_location(), evidence=self.make_evidence(), metadata={"rule_id": "CRT-SEC-999"})
        values.update(overrides)
        return Observation(**values)

    def test_import_and_immutable_nested_models(self):
        from dataclasses import FrozenInstanceError
        from src.observations import ObservationEvidence, ObservationLocation
        location = self.make_location(); evidence = self.make_evidence(); observation = self.make_observation()
        with self.assertRaises(FrozenInstanceError): location.path = "other.py"
        with self.assertRaises(FrozenInstanceError): evidence.length = 1
        with self.assertRaises(FrozenInstanceError): observation.kind = "other"

    def test_deterministic_ids_and_path_identity(self):
        one = self.make_observation()
        two = self.make_observation()
        self.assertEqual(one.observation_id, two.observation_id)
        self.assertRegex(one.observation_id, r"^obs-sha256:[0-9a-f]{64}$")
        self.assertEqual(one.observation_id, self.make_observation(location=self.make_location(path="other.py", identity_path="src/app.py")).observation_id)
        self.assertNotEqual(one.observation_id, self.make_observation(source_id="CRT-SEC-998").observation_id)
        self.assertNotEqual(one.observation_id, self.make_observation(location=self.make_location(start_line=8, end_line=8)).observation_id)
        self.assertNotIn("fixture-evidence-marker", one.observation_id)

    def test_evidence_is_redacted_and_deterministic(self):
        evidence = self.make_evidence()
        self.assertEqual(evidence, self.make_evidence())
        self.assertEqual(len(evidence.digest), 64)
        self.assertEqual(evidence.length, len("fixture-evidence-marker"))
        self.assertEqual(evidence.redacted, "[REDACTED]")
        self.assertNotIn("fixture-evidence-marker", json.dumps(evidence.to_dict()))
        self.assertNotIn("raw", evidence.to_dict())

    def test_validation(self):
        from src.observations import Observation, ObservationEvidence
        cases = [
            ("source_kind", "bad-source"), ("severity", "urgent"), ("confidence", "certain"),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                with self.assertRaises(ValueError): self.make_observation(**{field: value})
        with self.assertRaises(ValueError): self.make_location(start_line=-1)
        with self.assertRaises(ValueError): self.make_location(start_line=8, end_line=7)
        with self.assertRaises(ValueError): ObservationEvidence(redacted="[REDACTED]", digest="a" * 64, length=-1)
        with self.assertRaises(ValueError): ObservationEvidence(redacted="[REDACTED]", digest="a" * 64, length=10**9)

    def test_metadata_is_flat_json_safe(self):
        for bad in (b"bytes", Path("src/app.py"), ["list"], ("tuple",), {"set"}, {"nested": {"x": 1}}, lambda: None, object()):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(ValueError): self.make_observation(metadata={"bad": bad})
        observation = self.make_observation(metadata={"ok": "value", "n": 1, "flag": True, "none": None})
        self.assertEqual(observation.to_dict(), json.loads(json.dumps(observation.to_dict())))

    def test_model_has_no_scanner_import_or_side_effect_api(self):
        import ast
        from pathlib import Path
        import src.observations as observations
        tree = ast.parse(Path(observations.__file__).read_text(encoding="utf-8"))
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        self.assertNotIn("src.scanner", imports)
        self.assertFalse(hasattr(observations, "subprocess"))
        self.assertFalse(hasattr(observations, "socket"))


if __name__ == "__main__":
    unittest.main()
