"""Negative control: prove local_verify reports failures and exits non-zero.

This script monkeypatches one scenario with a deliberately wrong expectation
and drives the real _run_case entry point.  It does not modify any delivered
file or production code; the patch exists only in this process.
"""

import sys

sys.path.insert(0, ".")

from tests.dsse_threshold_support import SCENARIOS, ScenarioResult
from tools import local_verify


def broken_scenario(checker):
    checker.assert_equal(1, 2, "deliberately wrong equality")
    checker.assert_true(True, "this one passes")
    return ScenarioResult(
        name="c01_broken_control",
        seed={"note": "synthetic negative control"},
        expected={"x": 1},
        observed={"x": 2},
    )


original = SCENARIOS["C01"]
SCENARIOS["C01"] = original + [
    type(original[0])("c01_broken_control", "negative control", broken_scenario)
]
try:
    rc = local_verify.main(["--case", "C01"])
finally:
    SCENARIOS["C01"] = original

print("return code =", rc)
assert rc == 1, "harness must return non-zero when an assertion fails"
print("negative control OK: non-zero exit on failed assertion")
