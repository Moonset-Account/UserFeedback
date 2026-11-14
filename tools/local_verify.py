#!/usr/bin/env python3
# Local build/test wrapper for the Mach-O offline inspector (SEC-VULN-011).
# Rebuilds native C++17 tests and CLI from current sources using
# /usr/bin/clang++ via argv arrays (never shell=True), then runs the
# synthetic fixture test suite and CLI smoke checks. Non-zero exit on any
# compilation failure, zero tests, or failed assertion. Build artifacts
# stay inside this workspace (build/). No network access.
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")
CLANG = "/usr/bin/clang++"
STD = "-std=c++17"
WARN = ("-Wall", "-Wextra", "-Werror")

TEST_BIN = os.path.join(BUILD, "test_inspector")
CLI_BIN = os.path.join(BUILD, "macho_inspect")
LIB_OBJ = os.path.join(BUILD, "macho_inspect.o")
MAIN_OBJ = os.path.join(BUILD, "main.o")


def run(argv):
    print("+ " + " ".join(argv), flush=True)
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def main():
    os.makedirs(BUILD, exist_ok=True)
    lib = os.path.join(ROOT, "src", "macho_inspect.cpp")
    main_cpp = os.path.join(ROOT, "src", "main.cpp")
    tests = os.path.join(ROOT, "tests", "test_inspector.cpp")

    steps = [
        [CLANG, STD, WARN[0], WARN[1], WARN[2], "-c", lib, "-o", LIB_OBJ],
        [CLANG, STD, WARN[0], WARN[1], WARN[2], "-c", main_cpp, "-o", MAIN_OBJ],
        [CLANG, STD, MAIN_OBJ, LIB_OBJ, "-o", CLI_BIN],
        [CLANG, STD, WARN[0], WARN[1], WARN[2], lib, tests, "-o", TEST_BIN],
    ]
    for argv in steps:
        if run(argv) != 0:
            print("VERIFY: COMPILATION FAILED")
            return 2

    print("---- native unit tests ----", flush=True)
    rc = run([TEST_BIN])
    if rc != 0:
        print("VERIFY: UNIT TESTS FAILED rc=%d" % rc)
        return 3

    print("---- CLI smoke checks ----", flush=True)
    fixtures = [
        n for n in os.listdir(BUILD)
        if n.startswith("fixture_") and n.endswith(".bin")
    ]
    if len(fixtures) < 4:
        print("VERIFY: expected >=4 synthetic fixtures, got %d" % len(fixtures))
        return 4

    expectations = [
        ("fixture_TC01-thin-exec-compliant.bin", 0),
        ("fixture_TC02-fat-one-slice-missing-pie.bin", 1),
        ("fixture_TC03-truncated-load-command.bin", 2),
        ("fixture_TC04-fat-slice-descriptor-out-of-bounds.bin", 2),
        ("fixture_TC05-signature-payload-out-of-bounds.bin", 2),
    ]
    for name, want_rc in expectations:
        path = os.path.join(BUILD, name)
        if not os.path.exists(path):
            print("VERIFY: missing fixture " + name)
            return 4
        proc = subprocess.run([CLI_BIN, path], cwd=ROOT,
                              capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        if proc.returncode != want_rc:
            print("VERIFY: " + name + " expected exit " + str(want_rc)
                  + " got " + str(proc.returncode))
            return 5
        if name == "fixture_TC01-thin-exec-compliant.bin":
            if "code_signature_load=present" not in proc.stdout:
                print("VERIFY: TC01 report missing signature evidence")
                return 5
            if "pie=yes" not in proc.stdout:
                print("VERIFY: TC01 report missing PIE evidence")
                return 5

    print("VERIFY: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
