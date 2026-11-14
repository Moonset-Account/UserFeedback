#!/usr/bin/env python3
"""Local verification wrapper for SEC-VULN-017 (depth-limited event evaluator).

Recompiles the C++17 tests and CLI from current sources with
/usr/bin/clang++ (argv arrays, no shell), runs the native test suite, then
runs the CLI against a synthetic offline sample script and checks the
emitted result records. Exits non-zero on any compile failure, test
failure, missing test execution, or unexpected CLI output.

Usage:  python3 tools/local_verify.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLANG = "/usr/bin/clang++"
TIMEOUT_S = 60

SAMPLE_SCRIPT = """\
# Over-limit document followed by a valid one; batched via the script lines.
maxdepth 3
begin deep
enter a
enter b
enter c
enter d
end
begin shallow
enter x
field mode enforce
exit
end
"""

EXPECTED_LINES = [
    "FEED_STATUS Failed",
    "END_FAILED",
    "END_OK",
    "PUBLISH shallow fields=1",
    "FIELD /x/mode = enforce",
    "ERROR deep event=3 depth limit exceeded",
    "SUMMARY published=1 errors=1",
]
FORBIDDEN_LINES = ["PUBLISH deep"]


def run(argv, **kwargs):
    print("+ " + " ".join(str(a) for a in argv), flush=True)
    return subprocess.run(argv, timeout=TIMEOUT_S, **kwargs)


def main():
    failures = []
    build_dir = Path(tempfile.mkdtemp(prefix="sec-vuln-017-build-",
                                      dir=ROOT))
    test_bin = build_dir / "test_event_evaluator"
    cli_bin = build_dir / "policy_eval_cli"

    # 1. Compile the native test suite from current sources.
    r = run([CLANG, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
             str(ROOT / "src" / "event_evaluator.cpp"),
             str(ROOT / "tests" / "test_event_evaluator.cpp"),
             "-o", str(test_bin)])
    if r.returncode != 0:
        print("VERIFY FAIL: test compilation failed", flush=True)
        return 1

    # 2. Compile the CLI.
    r = run([CLANG, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
             str(ROOT / "src" / "event_evaluator.cpp"),
             str(ROOT / "src" / "main.cpp"),
             "-o", str(cli_bin)])
    if r.returncode != 0:
        print("VERIFY FAIL: CLI compilation failed", flush=True)
        return 1

    # 3. Run the native tests; require real executed checks.
    r = run([str(test_bin)], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        failures.append("native tests exited non-zero")
    if "RESULT PASS" not in r.stdout or "checks=0" in r.stdout:
        failures.append("native tests did not report a passing run")

    # 4. Run the CLI on the synthetic sample and check result records.
    r = run([str(cli_bin)], input=SAMPLE_SCRIPT,
            capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        failures.append("CLI exited non-zero")
    for line in EXPECTED_LINES:
        if line not in r.stdout:
            failures.append("CLI output missing: " + line)
    for line in FORBIDDEN_LINES:
        if line in r.stdout:
            failures.append("CLI output must not contain: " + line)

    if failures:
        for f in failures:
            print("VERIFY FAIL: " + f, flush=True)
        return 1
    print("VERIFY PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
