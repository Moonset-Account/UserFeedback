import sys

sys.path.insert(0, ".")
from tools.local_verify import AssertionFailure, Recorder

r = Recorder()
r.assert_equal(1, 1, "ok check")
r.assert_true(True, "true check")
r.assert_in("a", ["a", "b"], "in check")
assert r.passed == 3 and r.failed == 0

try:
    r.assert_equal(1, 2, "deliberate mismatch")
except AssertionFailure:
    pass
assert r.passed == 3 and r.failed == 1, (r.passed, r.failed)
print("recorder unit behavior OK:", r.passed, "passed,", r.failed, "failed")

with r.assert_raises(ValueError, "expect value error"):
    raise ValueError("boom")
assert r.passed == 4

try:
    with r.assert_raises(ValueError, "expect value error but runtime"):
        raise RuntimeError("nope")
except AssertionFailure:
    pass
assert r.failed == 2

try:
    with r.assert_raises(ValueError, "nothing raised"):
        pass
except AssertionFailure:
    pass
assert r.failed == 3
print("assert_raises behavior OK, total failed =", r.failed)
