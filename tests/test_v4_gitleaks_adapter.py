import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.adapters.gitleaks import import_gitleaks_report, run_gitleaks
from src.scanner import SecretScanner
from tests.synthetic_values import assemble


class V4GitleaksAdapterTests(unittest.TestCase):
    def write(self, root, name, data):
        path = Path(root) / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_imports_redacted_json_to_shared_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "report.json", [{
                "RuleID":"github-pat", "Description":"token", "File":"src/app.py",
                "StartLine":12, "Fingerprint":"src/app.py:github-pat:12", "Secret":"REDACTED", "Match":"REDACTED"
            }])
            findings = import_gitleaks_report(str(path))
            self.assertEqual(1, len(findings))
            item = findings[0]
            self.assertEqual("gitleaks", item.category)
            self.assertEqual("CRT-GL-001", item.rule_id)
            self.assertEqual("src/app.py", item.file)
            self.assertEqual(12, item.line)
            self.assertNotIn("token", item.matched_text.lower())

    def test_imports_sarif_and_deduplicates(self):
        result = {"ruleId":"generic-api-key","message":{"text":"redacted finding"},"locations":[{"physicalLocation":{"artifactLocation":{"uri":"lib/a.py"},"region":{"startLine":4}}}]}
        sarif = {"version":"2.1.0","runs":[{"tool":{"driver":{"name":"gitleaks"}},"results":[result,result]}]}
        with tempfile.TemporaryDirectory() as tmp:
            findings = import_gitleaks_report(str(self.write(tmp,"report.sarif",sarif)))
            self.assertEqual(1,len(findings)); self.assertEqual("lib/a.py",findings[0].file)

    def test_rejects_malformed_unsafe_or_secret_leaking_report(self):
        bad_reports = [
            [{"RuleID":"x","File":"../escape","StartLine":1,"Secret":"REDACTED","Match":"REDACTED"}],
            [{"RuleID":"x","File":"a.py","StartLine":1,"Secret":assemble("gh", "p_", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"),"Match":"REDACTED"}],
            [{"RuleID":"x","File":"a.py","StartLine":True,"Secret":"REDACTED","Match":"REDACTED"}],
            {"unknown":"shape"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index,data in enumerate(bad_reports):
                with self.subTest(index=index):
                    with self.assertRaises(ValueError):import_gitleaks_report(str(self.write(tmp,f"bad{index}.json",data)))

    def test_explicit_binary_version_and_execution_use_argument_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/"gitleaks"
            binary.write_text("#!/usr/bin/env python3\nimport pathlib,sys\nif sys.argv[1]=='version':\n print('8.21.0')\nelse:\n pathlib.Path(sys.argv[sys.argv.index('--report-path')+1]).write_text('[]')\n",encoding="utf-8"); binary.chmod(0o700)
            repo=Path(tmp)/"repo"; repo.mkdir()
            real_popen=__import__("subprocess").Popen
            with mock.patch("src.adapters.gitleaks.subprocess.Popen",wraps=real_popen) as popen:
                findings,version=run_gitleaks(str(binary),str(repo))
            for call in popen.call_args_list:
                self.assertIsInstance(call.args[0],list); self.assertFalse(call.kwargs.get("shell",False))
            self.assertEqual([],findings); self.assertEqual("8.21.0",version)

    def test_scanner_and_cli_import_report_into_shared_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=self.write(tmp,"report.json",[{"RuleID":"github-pat","File":"src/app.py","StartLine":2,"Secret":"REDACTED","Match":"REDACTED"}])
            result=SecretScanner().scan_gitleaks_report(str(report))
            self.assertEqual("gitleaks-report",result.input_type); self.assertEqual(1,len(result.findings))
            env=dict(os.environ); env["PYTHONPATH"]=str(Path(__file__).resolve().parents[1])
            proc=__import__("subprocess").run([__import__("sys").executable,"-m","src","scan","--gitleaks-report",str(report)],cwd=tmp,env=env,text=True,capture_output=True)
            self.assertEqual(1,proc.returncode)
            self.assertEqual("gitleaks-report",json.loads(proc.stdout)["input_type"])

    def test_refuses_relative_or_non_executable_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"gitleaks"; path.write_text("x",encoding="utf-8")
            with self.assertRaises(ValueError):run_gitleaks("gitleaks",tmp)
            with self.assertRaises(ValueError):run_gitleaks(str(path),tmp)


if __name__ == "__main__":unittest.main()
