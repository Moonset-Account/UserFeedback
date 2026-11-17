#!/usr/bin/env python3
"""Local verification entry point for the resource-derivation contract tests.

This wrapper runs the real unittest suites added in this change (C01-C04) and
records, per case, the number of assertions actually executed.  A case fails
with a non-zero exit code if any assertion fails/errors, if the suite has zero
assertions, or if the whole case is skipped - printing a success message alone
is never treated as passing.

Usage:
    python -I tools/local_verify.py --list
    python -I tools/local_verify.py --case C01
    python -I tools/local_verify.py --case C01 --case C02
    python -I tools/local_verify.py --case all

The wrapper deliberately inserts the workspace root on sys.path itself so it
works under CPython isolated mode (-I), where the current directory is not
imported implicitly.
"""

import argparse
import io
import json
import os
import sys
import unittest


WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)


CASES = {
    "C01": {
        "module": "tests.unit.test_resource_derivation_c01",
        "description": (
            "Finite S3 domain oracle: same-resource unconditional Deny "
            "overrides Allow; generated policies are checked against an "
            "independent small-domain model."
        ),
        "summary": (
            "oracle predicts allowed concrete object ARNs from Allow/Deny "
            "records; parliament.get_allowed_resources must equal the oracle"
        ),
    },
    "C02": {
        "module": "tests.unit.test_resource_derivation_c02",
        "description": (
            "Unconditional Deny on bare '*' clears the result to []; a Deny "
            "with a Condition is not treated as a universal negation."
        ),
        "summary": (
            "expect [] for Condition-less '*' Deny and preserved Allows for "
            "Condition-bearing Deny statements"
        ),
    },
    "C03": {
        "module": "tests.unit.test_resource_derivation_c03",
        "description": (
            "Statement reordering, scalar vs list Action/Resource, unrelated "
            "SQS/bucket resources, and explicit vs wildcard Action forms keep "
            "the same s3:GetObject resource derivation."
        ),
        "summary": (
            "identical allowed sets across permutations, representations and "
            "unrelated-resource additions"
        ),
    },
    "C04": {
        "module": "tests.unit.test_resource_derivation_c04",
        "description": (
            "ddmin failure-sample reduction keeps the S3 object resource type "
            "and unconditional Deny relationship with statement positions "
            "intact; ARN-glob intersection is documented as uncommitted."
        ),
        "summary": (
            "reduced policy is 1-minimal, object-only, preserves the target "
            "Deny and Sid order; glob/concrete keys never intersect"
        ),
    },
}

# Deterministic replay metadata surfaced in every JSON record. The synthetic
# fixtures use placeholder bucket names and the documentation example account
# id 123456789012 only; no credentials or private data are involved.
SEED_NOTE = {
    "C01": "random.Random(20261118) generates 8 reproducible Allow/Deny policies",
    "C02": "fixed inline fixtures; MFA condition uses aws:MultiFactorAuthPresent",
    "C03": "fixed inline fixtures; full permutation via itertools.permutations",
    "C04": "deterministic statement-level ddmin over a fixed 7-statement fixture",
}


class AssertionCountingResult(unittest.TextTestResult):
    """Counts real assert* calls instead of just test methods."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.assertion_count = 0
        self.method_counts = {}

    def addSuccess(self, test):
        # TestCase stores the running count on the instance itself.
        count = getattr(test, "_assertion_count", 0)
        self.method_counts[test.id()] = count
        self.assertion_count += count
        super().addSuccess(test)

    def addFailure(self, test, err):
        count = getattr(test, "_assertion_count", 0)
        self.method_counts[test.id()] = count
        self.assertion_count += count
        super().addFailure(test, err)

    def addError(self, test, err):
        count = getattr(test, "_assertion_count", 0)
        self.method_counts[test.id()] = count
        self.assertion_count += count
        super().addError(test, err)


def install_assertion_counter():
    """Wrap TestCase assertion methods so every call increments a counter."""

    counter_attr = "_assertion_count"
    method_names = [
        name
        for name in dir(unittest.TestCase)
        if name.startswith("assert")
        and callable(getattr(unittest.TestCase, name))
    ]

    def make_wrapper(original_name):
        original = getattr(unittest.TestCase, original_name)

        def wrapper(self, *args, **kwargs):
            setattr(
                self,
                counter_attr,
                getattr(self, counter_attr, 0) + 1,
            )
            return original(self, *args, **kwargs)

        wrapper.__name__ = original_name
        return wrapper

    for name in method_names:
        setattr(unittest.TestCase, name, make_wrapper(name))

    # setUp on every test starts the per-method count at zero.
    original_set_up = unittest.TestCase.setUp

    def setUp(self):  # noqa: N802 - unittest API name
        setattr(self, counter_attr, 0)
        original_set_up(self)

    unittest.TestCase.setUp = setUp


def run_case(case_id):
    spec = CASES[case_id]
    loader = unittest.TestLoader()
    try:
        suite = loader.loadTestsFromName(spec["module"])
    except Exception as exc:  # pragma: no cover - surfaced as a real failure
        return {
            "case": case_id,
            "assertions": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 1,
            "status": "error",
            "seed_or_fixture": SEED_NOTE[case_id],
            "expected_observed": "module could not be imported: {}".format(exc),
        }

    stream = io.StringIO()
    runner = unittest.TextTestRunner(
        stream=stream,
        resultclass=AssertionCountingResult,
        verbosity=2,
    )
    result = runner.run(suite)

    test_total = result.testsRun
    skipped = len(result.skipped)
    failed = len(result.failures) + len(result.errors)
    passed = test_total - failed - skipped
    assertions = result.assertion_count

    failures = []
    for kind, items in (
        ("failure", result.failures),
        ("error", result.errors),
    ):
        for test, traceback in items:
            # Keep only the final assertion line for the JSON summary; the
            # full traceback is printed to stderr for the operator.
            last_line = traceback.strip().splitlines()[-1] if traceback else ""
            failures.append(
                {
                    "kind": kind,
                    "test": test.id(),
                    "detail": last_line,
                }
            )

    zero_assertion_tests = [
        test_id
        for test_id, count in result.method_counts.items()
        if count == 0
    ]

    if failed or result.unexpectedSuccesses:
        status = "failed"
    elif test_total == 0:
        status = "empty"
    elif assertions == 0:
        status = "zero_assertions"
    elif skipped and passed == 0:
        status = "all_skipped"
    else:
        status = "passed"

    record = {
        "case": case_id,
        "assertions": assertions,
        "tests_run": test_total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "status": status,
        "seed_or_fixture": SEED_NOTE[case_id],
        "expected_observed": spec["summary"],
    }
    if zero_assertion_tests:
        record["zero_assertion_tests"] = zero_assertion_tests
    if failures:
        record["failures"] = failures

    # Emit the verbose unittest traceback for humans, JSON stays the contract.
    printed = stream.getvalue()
    if printed:
        sys.stderr.write(printed)
        if not printed.endswith("\n"):
            sys.stderr.write("\n")
    for test, traceback in result.failures + result.errors:
        sys.stderr.write(traceback + "\n")

    return record


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the C01-C04 resource-derivation contract cases."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the available cases and their descriptions.",
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=sorted(CASES.keys()) + ["all"],
        help="Case to run; repeat or pass 'all' for every case.",
    )
    args = parser.parse_args(argv)

    if args.list:
        listing = [
            {"case": case_id, "module": spec["module"], "description": spec["description"]}
            for case_id, spec in sorted(CASES.items())
        ]
        print(json.dumps({"cases": listing}, indent=2, sort_keys=True))
        return 0

    if not args.case:
        parser.error("use --list or choose at least one --case")

    selected = []
    for choice in args.case:
        if choice == "all":
            selected = sorted(CASES.keys())
            break
        if choice not in selected:
            selected.append(choice)

    install_assertion_counter()
    records = [run_case(case_id) for case_id in selected]
    print(json.dumps({"results": records}, indent=2, sort_keys=True))

    ok_statuses = {"passed"}
    success = all(
        record["status"] in ok_statuses
        and record["assertions"] > 0
        and record["passed"] > 0
        for record in records
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
