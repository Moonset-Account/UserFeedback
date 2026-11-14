"""输入加载与校验的单元测试。"""

import json
import os
import tempfile
import unittest

from aggregator.loader import LoaderError, load_bundle, load_parts, validate


def valid_parts():
    return (
        {"ticket": "T", "fixed_at": "2026-11-10T08:00:00Z",
         "components": [{"name": "svc", "fixed_artifact": "svc:2.0:b100"}]},
        {"instances": []},
        {"results": []},
        {"machines": []},
    )


class TestValidate(unittest.TestCase):

    def test_valid_minimal(self):
        fix, dep, reg, mac = valid_parts()
        data = validate(fix, dep, reg, mac)
        self.assertEqual(data["fix"]["ticket"], "T")

    def test_missing_components_rejected(self):
        fix, dep, reg, mac = valid_parts()
        del fix["components"]
        with self.assertRaises(LoaderError):
            validate(fix, dep, reg, mac)

    def test_duplicate_component_rejected(self):
        fix, dep, reg, mac = valid_parts()
        fix["components"].append({"name": "svc", "fixed_artifact": "svc:2.1:b101"})
        with self.assertRaises(LoaderError):
            validate(fix, dep, reg, mac)

    def test_bad_instance_state_rejected(self):
        fix, dep, reg, mac = valid_parts()
        dep["instances"].append(
            {"instance_id": "i-1", "component": "svc", "environment": "prod",
             "artifact": "a", "machine_id": "m-1", "state": "paused",
             "commissioned_at": "2026-11-10T09:00:00Z"})
        with self.assertRaises(LoaderError):
            validate(fix, dep, reg, mac)

    def test_decommissioned_instance_requires_timestamp(self):
        fix, dep, reg, mac = valid_parts()
        dep["instances"].append(
            {"instance_id": "i-1", "component": "svc", "environment": "prod",
             "artifact": "a", "machine_id": "m-1", "state": "decommissioned",
             "commissioned_at": "2026-11-10T09:00:00Z"})
        with self.assertRaises(LoaderError):
            validate(fix, dep, reg, mac)

    def test_bad_regression_phase_rejected(self):
        fix, dep, reg, mac = valid_parts()
        reg["results"].append(
            {"check_id": "C", "component": "svc", "env_category": "prod",
             "artifact": "a", "phase": "mid_fix", "result": "pass",
             "run_at": "2026-11-11T00:00:00Z"})
        with self.assertRaises(LoaderError):
            validate(fix, dep, reg, mac)

    def test_duplicate_machine_rejected(self):
        fix, dep, reg, mac = valid_parts()
        mach = {"machine_id": "m-1", "hostname": "h", "environment": "prod",
                "status": "online"}
        mac["machines"] = [mach, dict(mach)]
        with self.assertRaises(LoaderError):
            validate(fix, dep, reg, mac)


class TestLoadFiles(unittest.TestCase):

    def _write(self, directory, name, obj):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def test_load_bundle_roundtrip(self):
        fix, dep, reg, mac = valid_parts()
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "bundle.json",
                               {"fix": fix, "deployments": dep,
                                "regression": reg, "machines": mac})
            data = load_bundle(path)
            self.assertEqual(data["fix"]["ticket"], "T")

    def test_load_bundle_missing_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "bundle.json", {"fix": {}})
            with self.assertRaises(LoaderError):
                load_bundle(path)

    def test_load_parts_roundtrip(self):
        fix, dep, reg, mac = valid_parts()
        with tempfile.TemporaryDirectory() as tmp:
            paths = [self._write(tmp, name, obj) for name, obj in
                     zip(("fix.json", "dep.json", "reg.json", "mac.json"),
                         (fix, dep, reg, mac))]
            data = load_parts(*paths)
            self.assertEqual(data["fix"]["ticket"], "T")

    def test_missing_file_raises_loader_error(self):
        with self.assertRaises(LoaderError):
            load_bundle("/nonexistent/path/bundle.json")

    def test_invalid_json_raises_loader_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(LoaderError):
                load_bundle(path)


if __name__ == "__main__":
    unittest.main()
