"""Loader validation, malformed-config rejection and CLI end-to-end tests."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from vlancheck.analyzer import analyze
from vlancheck.cli import main as cli_main
from vlancheck.loader import load_config, port_ref_from_string
from vlancheck.model import ConfigError


VALID_CONFIG = {
    "ports": [
        {
            "device": "SW1",
            "port": "p1",
            "native_vlan": 10,
            "tagged_ingress_vlans": [20],
            "egress": {"10": "untag", "20": {"tag": 20}},
        },
        {
            "device": "SW2",
            "port": "p1",
            "native_vlan": 10,
            "tagged_ingress_vlans": [20],
            "egress": {"10": "untag", "20": {"tag": 20}},
        },
    ],
    "links": [["SW1", "p1", "SW2", "p1"]],
    "domain_pairs": [[["SW1", 10], ["SW2", 10]]],
}


class LoaderTests(unittest.TestCase):
    def test_load_valid_dict_and_analyze(self):
        topo = load_config(VALID_CONFIG)
        report = analyze(topo)
        self.assertIn(("SW1", 10), report.results)
        result = report.results[("SW1", 10)]
        self.assertEqual(result.boundary_events[0].status, "untagged_ok")

    def test_load_json_string(self):
        topo = load_config(json.dumps(VALID_CONFIG))
        self.assertEqual(len(topo.links), 1)

    def test_load_json_file_path(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as handle:
            json.dump(VALID_CONFIG, handle)
            path = handle.name
        try:
            topo = load_config(path)
            self.assertIn(("SW1", "p1"), topo.ports)
        finally:
            os.unlink(path)

    def test_egress_string_keys_accepted(self):
        topo = load_config(VALID_CONFIG)
        action = topo.ports[("SW1", "p1")].egress[20]
        self.assertEqual(action, ("tag", 20))

    def test_port_ref_parser(self):
        self.assertEqual(port_ref_from_string("SW1/ge0/1"), ("SW1", "ge0/1"))
        with self.assertRaises(ConfigError):
            port_ref_from_string("no-slash")

    def test_missing_port_field_rejected(self):
        bad = json.loads(json.dumps(VALID_CONFIG))
        del bad["ports"][0]["native_vlan"]
        with self.assertRaises(ConfigError):
            load_config(bad)

    def test_native_vlan_missing_from_egress_rejected(self):
        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["ports"][0]["egress"] = {"20": {"tag": 20}}
        with self.assertRaises(ConfigError):
            load_config(bad)

    def test_link_to_undefined_port_rejected(self):
        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["links"] = [["SW1", "p1", "SW9", "p1"]]
        with self.assertRaises(ConfigError):
            load_config(bad)

    def test_duplicate_link_rejected(self):
        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["links"] = [
            ["SW1", "p1", "SW2", "p1"],
            ["SW2", "p1", "SW1", "p1"],
        ]
        with self.assertRaises(ConfigError):
            load_config(bad)

    def test_bad_vlan_id_rejected(self):
        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["ports"][0]["native_vlan"] = 5000
        with self.assertRaises(ConfigError):
            load_config(bad)

    def test_pair_referencing_carried_domain_only(self):
        bad = json.loads(json.dumps(VALID_CONFIG))
        bad["domain_pairs"] = [[["SW1", 10], ["SW2", 777]]]
        with self.assertRaises(ConfigError):
            load_config(bad)

    def test_unknown_root_rejected_by_analyzer(self):
        topo = load_config(VALID_CONFIG)
        with self.assertRaises(KeyError):
            analyze(topo, roots=[("SW1", 999)])

    def test_explicit_roots_subset(self):
        topo = load_config(VALID_CONFIG)
        report = analyze(topo, roots=[("SW1", 10)])
        self.assertEqual(list(report.results), [("SW1", 10)])


class CliTests(unittest.TestCase):
    def _write_config(self, config):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        )
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_cli_clean_config_exit_zero(self):
        path = self._write_config(VALID_CONFIG)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main([path])
        self.assertEqual(code, 0)
        self.assertIn("SUMMARY", buf.getvalue())

    def test_cli_leak_exit_one(self):
        leaking = json.loads(json.dumps(VALID_CONFIG))
        # SW2 puts the paired-id VLAN into a different domain: SW2 vlan10 is
        # still named 10 but its native frames get interpreted there anyway;
        # to force a leak, pair SW1:10 with SW2:20 while cable stays untagged.
        leaking["domain_pairs"] = [[["SW1", 10], ["SW2", 20]]]
        path = self._write_config(leaking)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main([path])
        self.assertEqual(code, 1)
        self.assertIn("FIRST MISINTERPRETATION", buf.getvalue())

    def test_cli_json_output(self):
        path = self._write_config(VALID_CONFIG)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main([path, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("results", payload)
        self.assertFalse(payload["has_leak"])


if __name__ == "__main__":
    unittest.main()
