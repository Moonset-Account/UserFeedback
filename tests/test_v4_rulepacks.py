import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.rulepacks import install_rule_pack, load_rule_pack, load_trusted_keyring, rollback_rule_pack
from src.scanner import SecretScanner

PUBLIC_KEY="03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
SIGNATURE="6fb38fe7d885693ca63ef60d034b4d3f892ba81cb29e58dfb28e933e0c6f970d2f611a5120623160495361e27674a7133c048f938d8ccba1d89c99c9f3a40501"
PAYLOAD={"schema":"coderisktools.rule-pack","version":1,"key_id":"crt-test-2026","pack":{"pack_version":"2026.07.1","rules":[{"name":"SYNTHETIC_VENDOR_TOKEN","regex":"svt_[A-Za-z0-9]{24}","severity":"high","description":"Synthetic vendor token","rule_id":"CRT-SEC-901","category":"secret","confidence":"high","remediation":"Rotate the synthetic vendor token.","kind":"secret","file_globs":["**/*.py"]}]}}


class V4RulePackTests(unittest.TestCase):
    def envelope(self):return {**copy.deepcopy(PAYLOAD),"signature":SIGNATURE}
    def write(self,root,name,data):
        path=Path(root)/name; path.write_text(json.dumps(data),encoding="utf-8"); return path

    def test_valid_signature_loads_detection_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules=load_rule_pack(str(self.write(tmp,"pack.json",self.envelope())),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})
            self.assertEqual("CRT-SEC-901",rules[0].rule_id)
            self.assertIsNotNone(rules[0].compiled.search("svt_"+"A"*24))

    def test_unsigned_tampered_and_unknown_key_fail_closed(self):
        unsigned=copy.deepcopy(PAYLOAD)
        tampered=self.envelope(); tampered["pack"]["rules"][0]["severity"]="low"
        unknown=self.envelope(); unknown["key_id"]="unknown"
        with tempfile.TemporaryDirectory() as tmp:
            for index,data in enumerate((unsigned,tampered,unknown)):
                with self.subTest(index=index),self.assertRaises(ValueError):
                    load_rule_pack(str(self.write(tmp,f"bad{index}.json",data)),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})

    def test_unsafe_regex_is_rejected_after_valid_envelope_shape(self):
        unsafe=self.envelope(); unsafe["pack"]["rules"][0]["regex"]="(a+)+$"
        unsafe["signature"]="1277c8cc5474da5d806e14e675c9353575a06b4ea58247e2ba4a1d5f3cc89348842b0a7794737a96d47a21a3033a527ceb29b1baef465a74f706bdf187640c07"
        with tempfile.TemporaryDirectory() as tmp,self.assertRaises(ValueError):
            load_rule_pack(str(self.write(tmp,"unsafe.json",unsafe)),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})

    def test_keyring_and_pack_extend_scanner(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack=self.write(tmp,"pack.json",self.envelope())
            keyring=self.write(tmp,"keys.json",{"schema":"coderisktools.rule-keyring","version":1,"keys":{"crt-test-2026":PUBLIC_KEY}})
            keys=load_trusted_keyring(str(keyring))
            scanner=SecretScanner(rule_pack_path=str(pack),trusted_rule_keys=keys,config_check=False)
            diff='--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1 @@\n+value="svt_'+('A'*24)+'"\n'
            result=scanner.scan_diff_text(diff)
            self.assertTrue(any(item.rule_id=="CRT-SEC-901" for item in result.findings))

    def test_cli_installs_signed_pack_with_explicit_keyring(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=self.write(tmp,"source.json",self.envelope()); destination=Path(tmp)/"installed.json"
            keyring=self.write(tmp,"keys.json",{"schema":"coderisktools.rule-keyring","version":1,"keys":{"crt-test-2026":PUBLIC_KEY}})
            root=str(Path(__file__).resolve().parents[1]); env=dict(os.environ); env["PYTHONPATH"]=root
            proc=subprocess.run([sys.executable,"-m","src","rules","install","--source",str(source),"--destination",str(destination),"--keyring",str(keyring)],cwd=root,env=env,text=True,capture_output=True)
            self.assertEqual(0,proc.returncode,proc.stderr); self.assertTrue(destination.is_file())

    def test_atomic_install_preserves_destination_on_failure_and_supports_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=self.write(tmp,"source.json",self.envelope()); destination=Path(tmp)/"installed.json"
            install_rule_pack(str(source),str(destination),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})
            accepted=destination.read_bytes()
            bad=self.envelope(); bad["pack"]["pack_version"]="tampered"
            bad_source=self.write(tmp,"bad.json",bad)
            with self.assertRaises(ValueError):install_rule_pack(str(bad_source),str(destination),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})
            self.assertEqual(accepted,destination.read_bytes())
            install_rule_pack(str(source),str(destination),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})
            destination.write_text("corrupt local copy",encoding="utf-8")
            rollback_rule_pack(str(destination),{"crt-test-2026":bytes.fromhex(PUBLIC_KEY)})
            self.assertEqual(accepted,destination.read_bytes())


if __name__=="__main__":unittest.main()
