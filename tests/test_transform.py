"""Unit tests for the outbound alert JSONL converter.

Each test class maps to explicit requirements from the task brief; see
README.md for the requirement -> test mapping table.
"""

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
import transform  # noqa: E402

KEY_A_V1 = bytes.fromhex("aa10" * 16)
KEY_A_V2 = bytes.fromhex("bb20" * 16)
KEY_B_V1 = bytes.fromhex("cc30" * 16)

GATEWAY_POLICY = transform.load_policy({
    "source": "gateway",
    "keep": ["ts", "action", "src.ip", "dst.ip", "http.host"],
    "replace": [{"path": "user.name", "purpose": "account"}],
    "required": ["ts", "user.name"],
})

ENDPOINT_POLICY = transform.load_policy({
    "source": "endpoint",
    "keep": ["ts", "host", "process.name", "alerts.rule", "alerts.severity"],
    "replace": [
        {"path": "identity.user", "purpose": "account"},
        {"path": "alerts.owner", "purpose": "account"},
    ],
    "required": ["ts", "identity.user"],
})


def expected_codename(key_bytes, tenant, key_version, purpose, value):
    """Independent recomputation of the codename (no transform.py code)."""
    msg = "\x00".join([tenant, key_version, purpose, value]).encode("utf-8")
    return "cod_" + hmac.new(key_bytes, msg, hashlib.sha256).hexdigest()[:24]


def run_lines(source, policy, lines, key_bytes=KEY_A_V1,
              tenant="tenant-a", key_version="v1"):
    """Run app.run over text lines; return (output_records, error_entries)."""
    out_fh, err_fh = io.StringIO(), io.StringIO()
    app.run(source, policy, key_bytes, tenant, key_version,
            io.StringIO("".join(lines)), out_fh, err_fh)
    records = [json.loads(l) for l in out_fh.getvalue().splitlines()]
    errors = [json.loads(l) for l in err_fh.getvalue().splitlines()]
    return records, errors


class CrossSourceCodenameTests(unittest.TestCase):
    """Same tenant+key_version+purpose: same account -> same codename even
    when the two sources use different field paths."""

    def test_same_account_same_codename_across_sources(self):
        gw = transform.transform_record(
            {"ts": "t1", "user": {"name": "alice"}},
            GATEWAY_POLICY, KEY_A_V1, "tenant-a", "v1")
        ep = transform.transform_record(
            {"ts": "t2", "identity": {"user": "alice"}},
            ENDPOINT_POLICY, KEY_A_V1, "tenant-a", "v1")
        self.assertEqual(gw["user"]["name"], ep["identity"]["user"])
        self.assertEqual(
            gw["user"]["name"],
            expected_codename(KEY_A_V1, "tenant-a", "v1", "account", "alice"))

    def test_codename_format(self):
        gw = transform.transform_record(
            {"ts": "t1", "user": {"name": "alice"}},
            GATEWAY_POLICY, KEY_A_V1, "tenant-a", "v1")
        cod = gw["user"]["name"]
        self.assertTrue(cod.startswith("cod_"))
        self.assertEqual(len(cod), len("cod_") + 24)
        int(cod[4:], 16)  # suffix must be hex

    def test_codename_changes_with_tenant(self):
        rec = {"ts": "t1", "user": {"name": "alice"}}
        a = transform.transform_record(rec, GATEWAY_POLICY, KEY_A_V1,
                                       "tenant-a", "v1")["user"]["name"]
        b = transform.transform_record(rec, GATEWAY_POLICY, KEY_B_V1,
                                       "tenant-b", "v1")["user"]["name"]
        self.assertNotEqual(a, b)

    def test_codename_changes_with_key_version(self):
        rec = {"ts": "t1", "user": {"name": "alice"}}
        v1 = transform.transform_record(rec, GATEWAY_POLICY, KEY_A_V1,
                                        "tenant-a", "v1")["user"]["name"]
        v2 = transform.transform_record(rec, GATEWAY_POLICY, KEY_A_V2,
                                        "tenant-a", "v2")["user"]["name"]
        self.assertNotEqual(v1, v2)

    def test_codename_changes_with_purpose(self):
        policy = transform.load_policy({
            "source": "s",
            "keep": [],
            "replace": [
                {"path": "a", "purpose": "account"},
                {"path": "b", "purpose": "device"},
            ],
            "required": [],
        })
        out = transform.transform_record(
            {"a": "alice", "b": "alice"}, policy, KEY_A_V1, "tenant-a", "v1")
        self.assertNotEqual(out["a"], out["b"])


class FieldPolicyTests(unittest.TestCase):
    """Outbound records are built only from policy-declared fields."""

    def test_output_only_contains_declared_fields(self):
        out = transform.transform_record(
            {"ts": "t1", "action": "deny",
             "src": {"ip": "10.0.0.5", "port": 1234},
             "user": {"name": "alice", "uid": 777},
             "session_token": "s3cr3t"},
            GATEWAY_POLICY, KEY_A_V1, "tenant-a", "v1")
        self.assertEqual(set(out), {"ts", "action", "src", "user"})
        self.assertEqual(set(out["src"]), {"ip"})
        self.assertEqual(set(out["user"]), {"name"})
        self.assertNotIn("session_token", out)

    def test_nested_keep_path(self):
        out = transform.transform_record(
            {"ts": "t1", "user": {"name": "bob"},
             "http": {"host": "ex.example", "method": "GET"}},
            GATEWAY_POLICY, KEY_A_V1, "tenant-a", "v1")
        self.assertEqual(out["http"], {"host": "ex.example"})

    def test_array_elements_processed_by_same_rules(self):
        out = transform.transform_record(
            {"ts": "t1", "identity": {"user": "alice"},
             "alerts": [
                 {"rule": "r1", "severity": "high", "owner": "alice",
                  "debug": "drop-me"},
                 {"rule": "r2", "severity": "low", "owner": "carol"},
             ]},
            ENDPOINT_POLICY, KEY_A_V1, "tenant-a", "v1")
        alerts = out["alerts"]
        self.assertEqual(len(alerts), 2)
        # only declared fields survive inside array elements
        self.assertEqual(set(alerts[0]), {"rule", "severity", "owner"})
        self.assertEqual(set(alerts[1]), {"rule", "severity", "owner"})
        # same account string -> same codename inside arrays and at top level
        self.assertEqual(alerts[0]["owner"], out["identity"]["user"])
        self.assertNotEqual(alerts[0]["owner"], alerts[1]["owner"])
        self.assertEqual(
            alerts[1]["owner"],
            expected_codename(KEY_A_V1, "tenant-a", "v1", "account", "carol"))
    def test_empty_declared_array_preserved(self):
        out = transform.transform_record(
            {"ts": "t1", "identity": {"user": "dave"}, "alerts": []},
            ENDPOINT_POLICY, KEY_A_V1, "tenant-a", "v1")
        self.assertEqual(out["alerts"], [])


class PolicyTests(unittest.TestCase):
    """Per-source policy files exist, load, and are validated."""

    def test_shipped_policy_files_load(self):
        for name in ("gateway", "endpoint"):
            path = os.path.join(ROOT, "policies", name + ".json")
            with open(path, "r", encoding="utf-8") as fh:
                policy = transform.load_policy(json.load(fh))
            self.assertEqual(policy.source, name)
            self.assertTrue(policy.keep)
            self.assertTrue(policy.replace)
            self.assertTrue(policy.required)

    def test_keep_replace_overlap_rejected(self):
        with self.assertRaises(transform.PolicyError):
            transform.load_policy({
                "keep": ["a.b"],
                "replace": [{"path": "a.b", "purpose": "account"}],
            })

    def test_invalid_path_rejected(self):
        with self.assertRaises(transform.PolicyError):
            transform.load_policy({"keep": ["a..b"]})

    def test_replace_rule_needs_purpose(self):
        with self.assertRaises(transform.PolicyError):
            transform.load_policy({"replace": [{"path": "a"}]})


class SkipAndErrorTests(unittest.TestCase):
    """Missing required fields / type errors skip the line; the error
    report keeps only source, line and reason."""

    def test_missing_required_skips_line(self):
        records, errors = run_lines(
            "gateway", GATEWAY_POLICY,
            ['{"ts": "t1", "action": "deny"}\n',
             '{"ts": "t2", "user": {"name": "alice"}}\n'])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ts"], "t2")
        self.assertEqual(errors, [{"source": "gateway", "line": 1,
                                   "reason": "missing required field: user.name"}])

    def test_non_string_replace_value_skips(self):
        records, errors = run_lines(
            "gateway", GATEWAY_POLICY,
            ['{"ts": "t1", "user": {"name": 42}}\n'])
        self.assertEqual(records, [])
        self.assertEqual(errors, [{"source": "gateway", "line": 1,
                                   "reason": "non-string value at replace "
                                             "path: user.name"}])

    def test_invalid_json_line(self):
        records, errors = run_lines(
            "gateway", GATEWAY_POLICY, ["not json at all\n"])
        self.assertEqual(records, [])
        self.assertEqual(errors, [{"source": "gateway", "line": 1,
                                   "reason": "invalid JSON"}])

    def test_non_object_line_skips(self):
        records, errors = run_lines(
            "gateway", GATEWAY_POLICY, ['[1, 2, 3]\n'])
        self.assertEqual(records, [])
        self.assertEqual(errors, [{"source": "gateway", "line": 1,
                                   "reason": "line is not a JSON object"}])

    def test_error_report_entries_have_only_source_line_reason(self):
        lines = [
            '{"ts": "t1", "user": {"name": "alice"}}\n',   # ok
            '{"ts": "t2"}\n',                              # missing required
            '{"ts": "t3", "user": {"name": 9}}\n',         # type error
            'not json\n',                                  # invalid JSON
            '[1, 2]\n',                                    # not an object
        ]
        records, errors = run_lines("gateway", GATEWAY_POLICY, lines)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 4)
        for entry in errors:
            self.assertEqual(set(entry), {"source", "line", "reason"})
            self.assertEqual(entry["source"], "gateway")
        self.assertEqual([e["line"] for e in errors], [2, 3, 4, 5])
        self.assertEqual([e["reason"] for e in errors], [
            "missing required field: user.name",
            "non-string value at replace path: user.name",
            "invalid JSON",
            "line is not a JSON object",
        ])


class DeterminismTests(unittest.TestCase):
    """Identical input -> identical record order and codenames on rerun."""

    def test_rerun_produces_identical_output(self):
        lines = [
            '{"ts": "t1", "user": {"name": "alice"}, "action": "deny"}\n',
            '{"ts": "t2", "user": {"name": "bob"}, "action": "allow"}\n',
            '{"ts": "t3"}\n',
            '{"ts": "t4", "user": {"name": "alice"}, "action": "allow"}\n',
        ]
        runs = []
        for _ in range(2):
            out_fh, err_fh = io.StringIO(), io.StringIO()
            app.run("gateway", GATEWAY_POLICY, KEY_A_V1, "tenant-a", "v1",
                    io.StringIO("".join(lines)), out_fh, err_fh)
            runs.append((out_fh.getvalue(), err_fh.getvalue()))
        self.assertEqual(runs[0], runs[1])
        records = [json.loads(l) for l in runs[0][0].splitlines()]
        # record order follows input order; same account -> same codename
        self.assertEqual([r["ts"] for r in records], ["t1", "t2", "t4"])
        self.assertEqual(records[0]["user"]["name"],
                         records[2]["user"]["name"])


class CliEndToEndTests(unittest.TestCase):
    """End-to-end run of app.main with the shipped policies/keystore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keystore = os.path.join(ROOT, "keys.json")
        self.policies_dir = os.path.join(ROOT, "policies")
        self.task = os.path.join(ROOT, "samples", "task.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _run_cli(self, source, input_path):
        out = os.path.join(self.tmp.name, source + ".out")
        err = os.path.join(self.tmp.name, source + ".err")
        rc = app.main([
            "--source", source, "--task", self.task,
            "--input", input_path, "--output", out, "--errors", err,
            "--keystore", self.keystore, "--policies-dir", self.policies_dir,
        ])
        self.assertEqual(rc, 0)
        with open(out, "r", encoding="utf-8") as fh:
            records = [json.loads(l) for l in fh if l.strip()]
        with open(err, "r", encoding="utf-8") as fh:
            errors = [json.loads(l) for l in fh if l.strip()]
        return records, errors

    def test_cli_gateway_and_endpoint_share_codename(self):
        gw_records, gw_errors = self._run_cli(
            "gateway", os.path.join(ROOT, "samples", "gateway.jsonl"))
        ep_records, ep_errors = self._run_cli(
            "endpoint", os.path.join(ROOT, "samples", "endpoint.jsonl"))

        # gateway sample: lines 3 (missing user.name), 4 (non-string),
        # 5 (invalid JSON) are skipped
        self.assertEqual(len(gw_records), 2)
        self.assertEqual([e["line"] for e in gw_errors], [3, 4, 5])
        # endpoint sample: line 3 (missing ts) is skipped
        self.assertEqual(len(ep_records), 2)
        self.assertEqual([e["line"] for e in ep_errors], [3])

        # cross-source: same account "alice" -> same codename
        gw_alice = gw_records[0]["user"]["name"]
        self.assertEqual(gw_alice, ep_records[0]["identity"]["user"])
        self.assertEqual(gw_alice, ep_records[0]["alerts"][0]["owner"])
        self.assertEqual(
            gw_alice,
            expected_codename(KEY_A_V1, "tenant-a", "v1", "account", "alice"))

        # undeclared fields never leave the gateway
        self.assertNotIn("session_token", gw_records[0])
        self.assertEqual(set(gw_records[0]["user"]), {"name"})
        # array element objects follow the same whitelist
        self.assertEqual(set(ep_records[0]["alerts"][0]),
                         {"rule", "severity", "owner"})

    def test_cli_unknown_key_fails(self):
        with open(os.path.join(self.tmp.name, "task.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"tenant": "tenant-a", "key_version": "v9"}, fh)
        inp = os.path.join(self.tmp.name, "in.jsonl")
        with open(inp, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with self.assertRaises(SystemExit) as ctx:
            app.main([
                "--source", "gateway",
                "--task", os.path.join(self.tmp.name, "task.json"),
                "--input", inp,
                "--output", os.path.join(self.tmp.name, "o"),
                "--errors", os.path.join(self.tmp.name, "e"),
                "--keystore", self.keystore,
                "--policies-dir", self.policies_dir,
            ])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
