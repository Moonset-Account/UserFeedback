#!/usr/local/bin/python3
"""Local verification wrapper for the snapshot restore tool.

Steps:
  1. Discover the unittest suite with unittest.defaultTestLoader.discover("tests")
     and require at least one test case to be found.
  2. Actually run the suite and require at least one test to have really
     executed (not been skipped); zero discovered tests, all-skipped runs,
     errors or failures all exit non-zero.
  3. Run the delivered sample/boundary assertions for this task (the
     two-phase demo: corrupted last chunk keeps the target intact, intact
     backup replaces it) and print each case with its actual result.

Usage:  /usr/local/bin/python3 tools/local_verify.py
Exit code 0 only if the suite passes and every demo assertion passes.
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def run_unittest_suite():
    os.chdir(ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("[verify] discovered %d test case(s) in tests/" % count)
    if count < 1:
        print("[verify] FAIL: zero test cases discovered")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("[verify] testsRun=%d skipped=%d failures=%d errors=%d"
          % (result.testsRun, len(result.skipped), len(result.failures),
             len(result.errors)))
    if executed < 1:
        print("[verify] FAIL: no test actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[verify] FAIL: unittest suite reported failures/errors")
        return False
    return True


def run_demo_assertions():
    """The two-phase restore demonstration on one and the same target file."""
    import app

    tmp = tempfile.mkdtemp(prefix="snapdemo-")
    cases = []
    try:
        chunks_dir = os.path.join(tmp, "chunks")
        keys_dir = os.path.join(tmp, "keys")
        target = os.path.join(tmp, "live", "data.bin")
        os.makedirs(os.path.dirname(target))
        source = os.path.join(tmp, "source.bin")
        payload = bytes((i * 13 + 5) % 256 for i in range(200))
        with open(source, "wb") as fh:
            fh.write(payload)
        app.create_backup("acme", "snap-demo", "k1", source, chunks_dir, keys_dir,
                          chunk_size=48)
        live_content = b"live content that must survive a failed restore\n"
        with open(target, "wb") as fh:
            fh.write(live_content)

        snap_dir = os.path.join(chunks_dir, "snap-demo")
        chunk_names = sorted(n for n in os.listdir(snap_dir) if n != app.MANIFEST_NAME)
        last = chunk_names[-1]
        last_path = os.path.join(snap_dir, last)
        with open(last_path, encoding="utf-8") as fh:
            good_chunk = json.load(fh)

        # ---- case 1: corrupt the last chunk, target must be preserved ----
        corrupted = dict(good_chunk)
        ct = bytearray(base64.b64decode(corrupted["ciphertext"]))
        ct[-1] ^= 0xFF
        corrupted["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
        with open(last_path, "w", encoding="utf-8") as fh:
            json.dump(corrupted, fh)

        error = None
        try:
            app.restore_snapshot("acme", "snap-demo", target, chunks_dir, keys_dir)
        except app.RestoreError as exc:
            error = str(exc)
        with open(target, "rb") as fh:
            target_after = fh.read()
        temps = [f for f in os.listdir(os.path.dirname(target)) if ".restore-" in f]
        ok = (error is not None and "authentication failed" in error
              and target_after == live_content and temps == [])
        cases.append({
            "case_id": "DEMO-1-corrupted-last-chunk",
            "input": "tenant=acme snapshot=snap-demo, last chunk ciphertext corrupted",
            "expected": "restore fails with authentication reason; target bytes "
                        "unchanged; no temp files left",
            "actual": "error=%r target_unchanged=%s leftover_temp=%d"
                      % (error, target_after == live_content, len(temps)),
            "pass": ok,
        })

        # ---- case 2: intact backup, target must be fully replaced --------
        with open(last_path, "w", encoding="utf-8") as fh:
            json.dump(good_chunk, fh)
        written = app.restore_snapshot("acme", "snap-demo", target, chunks_dir, keys_dir)
        with open(target, "rb") as fh:
            restored = fh.read()
        temps = [f for f in os.listdir(os.path.dirname(target)) if ".restore-" in f]
        ok = (written == len(payload) and restored == payload and temps == [])
        cases.append({
            "case_id": "DEMO-2-intact-backup-replaces-target",
            "input": "tenant=acme snapshot=snap-demo, intact backup, same target file",
            "expected": "restore succeeds; target replaced by the %d original bytes"
                        % len(payload),
            "actual": "written=%d content_matches=%s leftover_temp=%d"
                      % (written, restored == payload, len(temps)),
            "pass": ok,
        })
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    all_ok = True
    for case in cases:
        all_ok = all_ok and case["pass"]
        print("[case %s]" % case["case_id"])
        print("  input:    %s" % case["input"])
        print("  expected: %s" % case["expected"])
        print("  actual:   %s" % case["actual"])
        print("  result:   %s" % ("PASS" if case["pass"] else "FAIL"))
    if len(cases) < 2:
        print("[verify] FAIL: demo did not produce both cases")
        return False
    return all_ok


def main():
    suite_ok = run_unittest_suite()
    demo_ok = run_demo_assertions()
    if suite_ok and demo_ok:
        print("[verify] OK: unittest suite passed and all demo assertions passed")
        return 0
    print("[verify] FAILED (suite_ok=%s demo_ok=%s)" % (suite_ok, demo_ok))
    return 1


if __name__ == "__main__":
    sys.exit(main())
