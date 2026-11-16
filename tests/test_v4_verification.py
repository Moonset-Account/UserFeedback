import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

from src.verification import CredentialVerifier, VerificationOutcome
from tests.synthetic_values import assemble


SYNTHETIC_GITHUB_CREDENTIAL = assemble("gh", "p_", "SYNTHETIC_NOT_REAL")


class Response:
    def __init__(self,status=200):self.status=status
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,limit=-1):return b'{"sensitive":"must-not-be-read"}'


class V4VerificationTests(unittest.TestCase):
    def test_offline_default_never_calls_network(self):
        verifier=CredentialVerifier()
        with mock.patch("src.verification.urllib.request.build_opener") as opener:
            outcome=verifier.verify("github",SYNTHETIC_GITHUB_CREDENTIAL,consent=False)
        opener.assert_not_called(); self.assertEqual("disabled",outcome.status)

    def test_enabled_still_requires_per_run_consent(self):
        verifier=CredentialVerifier(enabled=True)
        with mock.patch("src.verification.urllib.request.build_opener") as opener:
            outcome=verifier.verify("github","synthetic",consent=False)
        opener.assert_not_called(); self.assertEqual("consent_required",outcome.status)

    def test_allowlisted_request_has_timeout_no_redirect_and_redacted_outcome(self):
        verifier=CredentialVerifier(enabled=True,min_interval_seconds=0)
        opener=mock.Mock(); opener.open.return_value=Response(200)
        with mock.patch("src.verification.urllib.request.build_opener",return_value=opener):
            outcome=verifier.verify("github",SYNTHETIC_GITHUB_CREDENTIAL,consent=True)
        request=opener.open.call_args.args[0]
        self.assertEqual("api.github.com",request.host)
        timeout=opener.open.call_args.kwargs["timeout"]
        self.assertGreater(timeout,0); self.assertLessEqual(timeout,5)
        self.assertEqual("valid",outcome.status)
        serialized=json.dumps(outcome.to_dict())
        self.assertNotIn("ghp_",serialized); self.assertNotIn("sensitive",serialized)

    def test_invalid_and_network_errors_do_not_include_body_or_secret(self):
        verifier=CredentialVerifier(enabled=True,min_interval_seconds=0,max_retries=0)
        failures=[urllib.error.HTTPError("https://api.github.com/user",401,assemble("gh", "p_", "SYNTHETIC"),{},io.BytesIO(b"secret body")), OSError("synthetic credential leaked")]
        expected=["invalid","error"]
        for failure,status in zip(failures,expected):
            opener=mock.Mock(); opener.open.side_effect=failure
            with self.subTest(status=status),mock.patch("src.verification.urllib.request.build_opener",return_value=opener):
                outcome=verifier.verify("github",SYNTHETIC_GITHUB_CREDENTIAL,consent=True)
                text=json.dumps(outcome.to_dict())
                self.assertEqual(status,outcome.status); self.assertNotIn("ghp_",text); self.assertNotIn("secret body",text)

    def test_unknown_provider_and_invalid_limits_fail_closed(self):
        verifier=CredentialVerifier(enabled=True)
        with self.assertRaises(ValueError):verifier.verify("evil","x",consent=True)
        with self.assertRaises(ValueError):CredentialVerifier(enabled=True,max_retries=4)
        with self.assertRaises(ValueError):CredentialVerifier(enabled=True,timeout_seconds=0)

    def test_cli_requires_per_run_consent_without_leaking_env_value(self):
        env=dict(os.environ); env["CRT_SYNTHETIC_CREDENTIAL"]=SYNTHETIC_GITHUB_CREDENTIAL
        root=os.path.dirname(os.path.dirname(__file__)); env["PYTHONPATH"]=root
        proc=subprocess.run([sys.executable,"-m","src","verify","--provider","github","--credential-env","CRT_SYNTHETIC_CREDENTIAL"],cwd=root,env=env,text=True,capture_output=True)
        self.assertEqual(0,proc.returncode)
        data=json.loads(proc.stdout); self.assertEqual("consent_required",data["status"])
        self.assertNotIn("ghp_",proc.stdout+proc.stderr)

    def test_rate_limit_blocks_immediate_second_network_call(self):
        verifier=CredentialVerifier(enabled=True,min_interval_seconds=60)
        opener=mock.Mock(); opener.open.return_value=Response(200)
        with mock.patch("src.verification.urllib.request.build_opener",return_value=opener):
            first=verifier.verify("github","one",consent=True)
            second=verifier.verify("github","two",consent=True)
        self.assertEqual("valid",first.status); self.assertEqual("rate_limited",second.status)
        self.assertEqual(1,opener.open.call_count)


if __name__=="__main__":unittest.main()
