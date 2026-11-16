import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from src.adapters.gitleaks import import_gitleaks_report
from src.git_history import collect_history_diffs
from src.rulepacks import _validate_regex, install_rule_pack, verify_ed25519
from src.verification import CredentialVerifier
from tests.test_v4_rulepacks import PAYLOAD, PUBLIC_KEY, SIGNATURE
from tests.synthetic_values import assemble


class Stage5AuditRepairTests(unittest.TestCase):
    def test_git_history_never_executes_textconv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); marker=root/"executed"
            subprocess.run(["git","init","-q"],cwd=root,check=True)
            subprocess.run(["git","config","user.email","test@example.invalid"],cwd=root,check=True)
            subprocess.run(["git","config","user.name","Test"],cwd=root,check=True)
            driver=root/"driver.sh"; driver.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n"); driver.chmod(0o700)
            subprocess.run(["git","config","diff.evil.textconv",str(driver)],cwd=root,check=True)
            (root/".gitattributes").write_text("*.txt diff=evil\n"); (root/"a.txt").write_text("safe\n")
            subprocess.run(["git","add","."],cwd=root,check=True); subprocess.run(["git","commit","-qm","one"],cwd=root,check=True)
            collect_history_diffs(str(root),max_commits=1)
            self.assertFalse(marker.exists())

    def test_gitleaks_external_fingerprint_is_hashed_and_malformed_sarif_is_controlled(self):
        secret=assemble("gh", "p_", "A"*36)
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/"r.json"; report.write_text(json.dumps([{"RuleID":"x","File":"a.py","StartLine":1,"Fingerprint":secret,"Secret":"REDACTED","Match":"REDACTED"}]))
            findings=import_gitleaks_report(str(report)); rendered=json.dumps([asdict(f) for f in findings])
            self.assertNotIn(secret,rendered); self.assertNotIn("ghp_",rendered)
            sarif=Path(tmp)/"bad.sarif"; sarif.write_text(json.dumps({"version":"2.1.0","runs":[{"results":[{"ruleId":"x","message":{"text":"redacted"},"fingerprints":"wrong","locations":[{"physicalLocation":{"artifactLocation":{"uri":"a.py"},"region":{"startLine":1}}}]}]}]}))
            with self.assertRaises(ValueError):import_gitleaks_report(str(sarif))

    def test_gitleaks_report_size_checked_without_path_read_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=Path(tmp)/"r.json"; report.write_text("[]")
            with mock.patch.object(Path,"read_bytes",side_effect=AssertionError("unsafe second/path read")):
                self.assertEqual([],import_gitleaks_report(str(report)))

    def test_verification_rejects_non_bool_and_disables_environment_proxies(self):
        with self.assertRaises(ValueError):CredentialVerifier(enabled="false")
        verifier=CredentialVerifier(enabled=True,min_interval_seconds=0,max_retries=0)
        with self.assertRaises(ValueError):verifier.verify("github","synthetic",consent="yes")
        with mock.patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value=mock.MagicMock(status=200)
            verifier.verify("github","synthetic",consent=True)
            handlers=build.call_args.args
            self.assertTrue(any(type(h).__name__=="ProxyHandler" and getattr(h,"proxies",None)=={} for h in handlers))

    def test_verification_deadline_caps_first_attempt_and_zero_interval_retries(self):
        verifier=CredentialVerifier(enabled=True,timeout_seconds=5,max_retries=0,min_interval_seconds=0)
        opener=mock.Mock(); response=mock.MagicMock(); response.__enter__.return_value.status=200; opener.open.return_value=response
        with mock.patch("src.verification.urllib.request.build_opener",return_value=opener),mock.patch("src.verification.time.monotonic",side_effect=[100.0,101.25]):
            self.assertEqual("valid",verifier.verify("github","synthetic",consent=True).status)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"],3.75)
        retry=CredentialVerifier(enabled=True,timeout_seconds=5,max_retries=2,min_interval_seconds=0)
        second=mock.Mock(); second_response=mock.MagicMock(); second_response.__enter__.return_value.status=200; second.open.side_effect=[OSError("transient"),second_response]
        with mock.patch("src.verification.urllib.request.build_opener",return_value=second):
            outcome=retry.verify("github","synthetic",consent=True)
        self.assertEqual("valid",outcome.status); self.assertEqual(2,outcome.attempts); self.assertEqual(2,second.open.call_count)

    def test_identity_key_signature_and_unsafe_regex_fail_closed(self):
        identity=b"\x01"+b"\x00"*31; forged=identity+b"\x00"*32
        self.assertFalse(verify_ed25519(identity,b"anything",forged))
        for regex in ("(a|aa)+$","(a{1,3})+$","(a{1,})+b","a?a?a?a?a?a?a?a?a?a?$","a{1,}"):
            with self.subTest(regex=regex),self.assertRaises(ValueError):_validate_regex(regex)

    def test_install_uses_verified_bytes_without_path_read_bytes(self):
        envelope={**PAYLOAD,"signature":SIGNATURE}
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"source.json"; source.write_text(json.dumps(envelope)); destination=Path(tmp)/"dest.json"
            with mock.patch.object(Path,"read_bytes",side_effect=AssertionError("TOCTOU read")):
                install_rule_pack(str(source),str(destination),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})
            self.assertTrue(destination.is_file())


if __name__=="__main__":unittest.main()
