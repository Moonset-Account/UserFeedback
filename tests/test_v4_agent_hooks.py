import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from src.hooks import parse_hook_payload
from tests.synthetic_values import assemble

GITHUB_PAT = assemble("gh", "p_", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
SECRET_DIFF=f'--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+token="{GITHUB_PAT}"\n'


class V4AgentHookTests(unittest.TestCase):
    def test_parses_generic_codex_and_claude_contracts(self):
        cases=[
            ("generic",{"schema":"coderisktools.agent-hook","version":1,"event":"post-change","diff":SECRET_DIFF}),
            ("codex",{"schema":"coderisktools.codex-hook","version":1,"event":"post-change","diff":SECRET_DIFF}),
            ("claude-code",{"schema":"coderisktools.claude-hook","version":1,"hook_event_name":"Stop","diff":SECRET_DIFF}),
        ]
        for agent,payload in cases:
            with self.subTest(agent=agent):self.assertEqual(SECRET_DIFF,parse_hook_payload(agent,json.dumps(payload).encode()))

    def test_rejects_malformed_unknown_duplicate_and_oversized_payload(self):
        bad=[b'{}',b'{"schema":"x","schema":"x","version":1}',b'not-json',b'x'*(4*1024*1024+1)]
        for payload in bad:
            with self.subTest(size=len(payload)),self.assertRaises(ValueError):parse_hook_payload("generic",payload)
        with self.assertRaises(ValueError):parse_hook_payload("unknown",b'{}')

    def test_cli_scans_stdin_redacts_secret_and_preserves_exit_code(self):
        root=str(Path(__file__).resolve().parents[1]); env=dict(os.environ); env["PYTHONPATH"]=root
        payload=json.dumps({"schema":"coderisktools.agent-hook","version":1,"event":"post-change","diff":SECRET_DIFF})
        proc=subprocess.run([sys.executable,"-m","src","hook","--agent","generic"],cwd=root,env=env,input=payload,text=True,capture_output=True)
        self.assertEqual(1,proc.returncode)
        data=json.loads(proc.stdout); self.assertEqual("agent-hook",data["input_type"])
        self.assertNotIn("ghp_",proc.stdout+proc.stderr)
        self.assertEqual("[REDACTED]",data["findings"][0]["matched_text"])

    def test_cli_malformed_input_returns_three_without_echo(self):
        root=str(Path(__file__).resolve().parents[1]); env=dict(os.environ); env["PYTHONPATH"]=root
        proc=subprocess.run([sys.executable,"-m","src","hook","--agent","generic"],cwd=root,env=env,input='{"secret":"' + assemble("gh", "p_", "SHOULD_NOT_ECHO") + '"}',text=True,capture_output=True)
        self.assertEqual(3,proc.returncode); self.assertNotIn("ghp_",proc.stdout+proc.stderr)

    def test_documented_command_flow_exists(self):
        doc=Path(__file__).resolve().parents[1]/"integrations"/"AI_AGENT_HOOKS.md"
        text=doc.read_text(encoding="utf-8")
        for marker in ("Codex","Claude Code","generic","stdin","Exit codes","offline"):
            self.assertIn(marker,text)


if __name__=="__main__":unittest.main()
