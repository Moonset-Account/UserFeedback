import sys

sys.path.insert(0, ".")

from tests.dsse_threshold_support import SCENARIOS, Scenario, ScenarioResult
from tools import local_verify


def zero_check(checker):
    # Intentionally performs no assertions.
    return ScenarioResult("zero", {}, {}, {})


SCENARIOS["ZZZ"] = [Scenario("zero", "no assertions", zero_check)]
rc = local_verify.main(["--case", "ZZZ"])
del SCENARIOS["ZZZ"]
print("zero-assertion rc =", rc)
assert rc == 1
print("zero-assertion guard OK")
