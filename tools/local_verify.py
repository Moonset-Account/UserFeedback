#!/usr/bin/env python3
"""Local compile/run wrapper for the SEC-VULN-013 stack-protection checker.

Standard library only. Recompiles the C++17 checker and its native tests from
the current sources with /usr/bin/clang++ on every invocation (no cached
binaries are trusted), then runs:

  1. the native assertion test suite (C++ tests, stable case ids)
  2. CLI end-to-end runs against the synthetic offline fixtures, asserting
     concrete verdicts, reasons, diffs and review units
  3. negative CLI runs (malformed input, missing file, --strict exit codes)

All temporary build outputs stay inside this workspace's .build/ directory.
No network access, no third-party packages.

Usage:
    python3 -I tools/local_verify.py
Exit code is non-zero on any compilation failure, zero executed tests or
failed assertion.
"""

import os
import subprocess
import sys
import tempfile

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(WORKSPACE, "src")
TESTS = os.path.join(WORKSPACE, "tests")
FIXTURES = os.path.join(TESTS, "fixtures")
BUILD = os.path.join(WORKSPACE, ".build")
CLANG = "/usr/bin/clang++"

LIB_SOURCES = [
    os.path.join(SRC, "ssp_checker", "Types.cpp"),
    os.path.join(SRC, "ssp_checker", "Parser.cpp"),
    os.path.join(SRC, "ssp_checker", "Analysis.cpp"),
    os.path.join(SRC, "ssp_checker", "Report.cpp"),
]

checks_run = 0
failures = []


def record(condition, label, detail=""):
    global checks_run
    checks_run += 1
    if condition:
        print("  ASSERT PASS: {0}".format(label))
    else:
        failures.append((label, detail))
        print("  ASSERT FAIL: {0} {1}".format(label, detail))


def run(argv, expect_exit=0, stdin_text=None):
    """Run a command via an argv array (never shell=True)."""
    proc = subprocess.run(
        argv,
        cwd=WORKSPACE,
        input=stdin_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if proc.returncode != expect_exit:
        raise AssertionError(
            "command {0} exited {1}, expected {2}\n--- stdout ---\n{3}\n"
            "--- stderr ---\n{4}".format(
                argv, proc.returncode, expect_exit, proc.stdout, proc.stderr
            )
        )
    return proc


def compile_target(sources, output_path, extra_flags=None):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    argv = [CLANG, "-std=c++17", "-Wall", "-Wextra", "-Wpedantic", "-Werror",
            "-I" + SRC]
    if extra_flags:
        argv.extend(extra_flags)
    argv.extend(sources)
    argv.extend(["-o", output_path])
    print("[build] " + " ".join(argv))
    subprocess.run(argv, cwd=WORKSPACE, check=True)


def main():
    os.makedirs(BUILD, exist_ok=True)

    # --- 1. fresh compilation from current sources ------------------------
    test_binary = os.path.join(BUILD, "test_ssp_checker")
    cli_binary = os.path.join(BUILD, "ssp-cover-check")
    if os.path.exists(test_binary):
        os.remove(test_binary)
    if os.path.exists(cli_binary):
        os.remove(cli_binary)

    compile_target(LIB_SOURCES + [os.path.join(TESTS, "test_ssp_checker.cpp")],
                   test_binary)
    compile_target(LIB_SOURCES + [os.path.join(SRC, "main.cpp")], cli_binary)

    # --- 2. native C++ assertion suite ------------------------------------
    print("\n[step 1] running native C++17 assertion suite")
    proc = run([test_binary])
    print(proc.stdout)
    native_passed = proc.stdout.count("[       OK ]")
    record(native_passed == 15, "native test case count == 15",
           "got {0}".format(native_passed))
    record("0 failure(s)" in proc.stdout, "native suite reports zero failures",
           proc.stdout)

    # --- 3. CLI end-to-end on synthetic two-config fixtures ---------------
    print("\n[step 2] CLI end-to-end run on O0 + O2 synthetic fixtures")
    risk = os.path.join(FIXTURES, "risk_list.txt")
    o0_attr = os.path.join(FIXTURES, "O0_attribution.txt")
    o0_marks = os.path.join(FIXTURES, "O0_marks.txt")
    o2_attr = os.path.join(FIXTURES, "O2_attribution.txt")
    o2_marks = os.path.join(FIXTURES, "O2_marks.txt")
    report_path = os.path.join(BUILD, "coverage_report.txt")
    if os.path.exists(report_path):
        os.remove(report_path)

    proc = run([
        cli_binary,
        "--risk-list", risk,
        "--bundle", "O0={0},{1}".format(o0_attr, o0_marks),
        "--bundle", "O2={0},{1}".format(o2_attr, o2_marks),
        "--report", report_path,
    ])
    report = proc.stdout
    record(os.path.exists(report_path), "report file written to --report path")
    with open(report_path, "r", encoding="utf-8") as handle:
        file_report = handle.read()
    record(file_report == report, "stdout and --report contents match")

    # Per-config summaries.
    record("S | config=O0 total=8 confirmed=7 violation=0 unknown=0 not_required=1"
           in report, "TC-CLI-001 O0 summary: 7 confirmed + 1 exempt leaf")
    record("S | config=O2 total=8 confirmed=3 violation=2 unknown=2 not_required=1"
           in report, "TC-CLI-002 O2 summary: 3 confirmed, 2 violation, 2 unknown")

    # Self-protected retained function.
    record("V | config=O2 risk_id=R001" in report and
           "verdict=CONFIRMED reason=self_protected" in report,
           "TC-CLI-003 R001 retained self record is protected")

    # Inlined source carried by a protected caller.
    record("V | config=O2 risk_id=R002" in report and
           "verdict=CONFIRMED reason=inlined_caller_protected" in report,
           "TC-CLI-004 R002 inlined into protected parse_packet_header")

    # Exempt leaf (even though its O2 attribution is multi-caller).
    record("V | config=O2 risk_id=R005" in report and
           "verdict=NOT_REQUIRED reason=exempt_leaf" in report,
           "TC-CLI-005 R005 policy-exempt leaf is not required")

    # Unprotected self violation.
    record("V | config=O2 risk_id=R003" in report and
           "verdict=VIOLATION reason=unprotected_self" in report,
           "TC-CLI-006 R003 retained self record lost protection")

    # Partial multi-caller protection -> unknown.
    record("V | config=O2 risk_id=R007" in report and
           "verdict=UNKNOWN reason=partial_caller_protection" in report,
           "TC-CLI-007 R007 split caller protection stays unknown")
    record("U | config=O2 cu=net/udp.c unknown=1" in report,
           "TC-CLI-008 unprotected carrying unit net/udp.c queued for review")

    # Missing protection mark -> unknown (R008 attributed, no O2 mark).
    # R008 IS present in O2 marks (M108); build a separate missing-mark case.
    # Minimal evidence chain content.
    record("evidence: A102 (inlined) decode_length_field -> parse_packet_header "
           "@ net/parse.c | mark=protected via M101 (ssp-strong)" in report,
           "TC-CLI-009 minimal evidence chain shows source -> owner -> mark ids")
    record("evidence: A107b (inlined) copy_options_block -> handle_udp_segment "
           "@ net/udp.c | mark=unprotected via M107 (-)" in report,
           "TC-CLI-010 evidence chain names the unprotected second caller")

    # Cross-config differences include the known regressions.
    record("D | risk_id=R003" in report and
           "O0:CONFIRMED/self_protected -> O2:VIOLATION/unprotected_self" in report,
           "TC-CLI-011 R003 optimization regression appears in diff section")
    record("D | risk_id=R007" in report and
           "O0:CONFIRMED/self_protected -> O2:UNKNOWN/partial_caller_protection"
           in report,
           "TC-CLI-012 R007 confirmation-to-unknown regression appears in diffs")

    # --- 4. missing-mark synthetic case -----------------------------------
    print("\n[step 3] missing protection mark scenario")
    with tempfile.TemporaryDirectory(prefix="ssp_verify_", dir=BUILD) as tmp:
        risk_path = os.path.join(tmp, "risk.txt")
        attr_path = os.path.join(tmp, "attr.txt")
        marks_path = os.path.join(tmp, "marks.txt")
        with open(risk_path, "w", encoding="utf-8") as handle:
            handle.write("R9|orphan|src/orphan.c|high|required|no mark exported\n")
        with open(attr_path, "w", encoding="utf-8") as handle:
            handle.write("O2|orphan|orphan|src/orphan.c|self|A900\n")
        with open(marks_path, "w", encoding="utf-8") as handle:
            handle.write("O2|other|src/other.c|protected|ssp|M900\n")
        proc = run([
            cli_binary, "--risk-list", risk_path,
            "--bundle", "O2={0},{1}".format(attr_path, marks_path),
        ])
        record("verdict=UNKNOWN reason=missing_protection_mark" in proc.stdout,
               "TC-CLI-013 attributed function without mark is unknown")
        record("mark=MISSING" in proc.stdout,
               "TC-CLI-014 evidence chain renders mark=MISSING")

    # --- 5. negative cases -------------------------------------------------
    print("\n[step 4] negative / strict-mode cases")
    with tempfile.TemporaryDirectory(prefix="ssp_verify_", dir=BUILD) as tmp:
        bad_path = os.path.join(tmp, "bad_risk.txt")
        with open(bad_path, "w", encoding="utf-8") as handle:
            handle.write("R1|alpha|a.c|extreme|required|bad enum row\n")
        proc = run([
            cli_binary, "--risk-list", bad_path,
            "--bundle", "O2={0},{1}".format(o2_attr, o2_marks),
        ], expect_exit=1)
        record("parse error" in proc.stderr and "line 1" in proc.stderr,
               "TC-CLI-015 malformed risk row fails with line-numbered error",
               proc.stderr)

        missing = os.path.join(tmp, "does_not_exist.txt")
        proc = run([
            cli_binary, "--risk-list", missing,
            "--bundle", "O2={0},{1}".format(o2_attr, o2_marks),
        ], expect_exit=1)
        record("cannot open risk list" in proc.stderr,
               "TC-CLI-016 missing input file exits 1", proc.stderr)

    # --strict: O0 alone is clean (exit 0); O0+O2 has violations (exit 3).
    proc = run([
        cli_binary, "--strict", "--risk-list", risk,
        "--bundle", "O0={0},{1}".format(o0_attr, o0_marks),
    ], expect_exit=0)
    record(True, "TC-CLI-017 --strict clean O0 build exits 0")

    proc = run([
        cli_binary, "--strict", "--risk-list", risk,
        "--bundle", "O0={0},{1}".format(o0_attr, o0_marks),
        "--bundle", "O2={0},{1}".format(o2_attr, o2_marks),
    ], expect_exit=3)
    record(True, "TC-CLI-018 --strict returns 3 when violations exist")

    # --- summary -----------------------------------------------------------
    print("\n=== local_verify summary: {0} python assertions, {1} failure(s) ==="
          .format(checks_run, len(failures)))
    if failures:
        for label, detail in failures:
            print("FAILED: {0} {1}".format(label, detail))
        return 1
    if checks_run < 1:
        print("no assertions were executed")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        print("compilation failed: {0}".format(exc), file=sys.stderr)
        sys.exit(1)
    except AssertionError as exc:
        print("command assertion failed: {0}".format(exc), file=sys.stderr)
        sys.exit(1)
