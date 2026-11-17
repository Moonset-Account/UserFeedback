# Resource-derivation contract verification (C01-C04)

Local, offline verification for the resource-derivation contract implemented by
Parliament's `Policy.get_allowed_resources` and
`Statement.get_resources_for_privilege`. Everything here is synthetic: the
fixtures use placeholder bucket names (`b-alpha`, `b-beta`, `b-gamma`) and the
documentation example account id `123456789012` only. No credentials, real
account data, network calls, or global packages are required.

## What is actually being tested

The tests pin the resource-derivation behavior the **current implementation**
commits to; they do not treat Parliament as a complete IAM authorizer.

| Case | Scope |
| ---- | ----- |
| C01 | Finite S3 domain with an independent small-domain oracle. An unconditional `Deny` on the same exact resource string overrides an `Allow`; the expected set is computed by `tests/support/resource_contracts.py`, not by Parliament. |
| C02 | An unconditional `Deny` on the bare `"*"` resource clears the result to `[]`; a `Deny` carrying a `Condition` is not treated as a negation that holds in every context. |
| C03 | Statement reordering (full permutation), scalar vs single-element-list `Action`/`Resource`, unrelated SQS statement and bucket ARN additions, and explicit (`s3:GetObject`) vs wildcard (`s3:Get*`, `s3:*`, `"*"`) Action forms. |
| C04 | Deterministic statement-level ddmin reduction of a failure sample that keeps the S3 *object* resource type and unconditional Deny relationship, while surviving statements keep their relative position/Sid; ARN-glob intersection is characterized as an uncommitted boundary. |

## APIs exercised (real entry points, no reimplementation)

- `parliament.analyze_policy_string(policy_str)` -> `Policy`
- `parliament.policy.Policy.get_allowed_resources(privilege_prefix, privilege_name)`
- `parliament.policy.Policy.get_references(privilege_prefix, privilege_name)`
- `parliament.statement.Statement.get_resources_for_privilege(privilege_prefix, privilege_name)`

The independent oracle and reducer live in
`tests/support/resource_contracts.py` and contain no Parliament imports for
their expected-value logic; they model only concrete S3 object ARNs and the
bare `"*"` resource.

## Commands

Run from the workspace root with the prepared virtualenv on `PATH`. Isolated
mode (`-I`) is supported because `tools/local_verify.py` inserts the workspace
root on `sys.path` itself.

```bash
# List the cases
python -I tools/local_verify.py --list

# Run one case, or several, or all of them
python -I tools/local_verify.py --case C01
python -I tools/local_verify.py --case C01 --case C02
python -I tools/local_verify.py --case all
```

The same suites are plain `unittest` modules and can be run without the wrapper
(note: the pinned `nose==1.3.7` in `requirements.txt` is incompatible with
Python 3.11 because it imports `collections.Callable`, so `unittest` is the
working runner in this environment):

```bash
python -m unittest tests/unit/test_resource_derivation_c01.py
python -m unittest tests/unit/test_resource_derivation_c02.py
python -m unittest tests/unit/test_resource_derivation_c03.py
python -m unittest tests/unit/test_resource_derivation_c04.py
```

### Exit codes and JSON record

`local_verify.py` exits non-zero if any assertion fails or errors, if a case
module cannot be imported, if a case executes **zero assertions**, or if an
entire case is skipped. A printed success line alone is never sufficient.

Each case emits a JSON record containing `case`, the actual assertion count
(`assertions`), `passed`/`failed`/`skipped`, `tests_run`, a `status`, the
replay seed/fixture description, and an expected-vs-observed summary. Verbose
unittest tracebacks go to stderr; the JSON goes to stdout. Current counts:

| Case | Test methods | Real assertions |
| ---- | ------------ | --------------- |
| C01  | 6  | 73 |
| C02  | 7  | 47 |
| C03  | 12 | 73 |
| C04  | 8  | 50 |

## Replayable samples

- **C01 generated policies**: `random.Random(20261118)` drives 8 reproducible
  Allow/Deny combinations over the four fixed object ARNs in
  `OBJECTS`. Re-running with the same seed produces byte-identical policies.
- **C02/C03 fixtures**: inline, deterministic policy builders
  (`render_policy`, `DStmt`) with explicit Sids; C03 additionally enumerates
  every `itertools.permutations` ordering of a four-statement policy.
- **C04 failure sample**: `build_reduction_input(target)` in
  `tests/support/resource_contracts.py` is a fixed seven-statement policy.
  `ddmin_statements(...)` deterministically reduces it to the 1-minimal pair
  `[BroadAllow, TargetDeny]`; the reducer itself asserts 1-minimality.

## Contract details worth knowing when reading failures

- Resources are grouped by their **exact resource string** in
  `get_references`/`get_allowed_resources`. Two different strings that could
  describe overlapping ARNs (e.g. `arn:aws:s3:::b-alpha/*` and
  `arn:aws:s3:::b-alpha/logs/a.txt`) never intersect inside Parliament.
- Only the literal `"*"` resource has special clear-to-empty handling, and
  only when the referencing `Deny` has no `Condition`. An ARN-shaped wildcard
  does not trigger that branch.
- `Statement.get_resources_for_privilege` can return `"*"` twice for a
  `"*"` resource (once via the resource-type loop, once via the explicit
  `"*"` loop). The tests compare sets, matching the library's dict-key usage.
- `s3:GetObject` applies to the `object*` resource type only; a bucket ARN
  without a slash is excluded by design, while `s3:ListBucket` matches bucket
  ARNs. This comes from the bundled `iam_definition.json`.

## Uncovered / explicitly uncommitted boundaries

These are intentionally not asserted as guaranteed behavior, and are either
documented as current-characterization tests (C04) or left out entirely:

1. **Arbitrary ARN-pattern intersection.** There is no promised semantics for
   intersecting two glob ARN patterns or a glob pattern with a concrete ARN.
   `TestArbitraryArnIntersectionIsNotPromised` records the current
   independent-key behavior only.
2. **`NotResource` / `NotAction`.** `get_resources_for_privilege` returns
   `["*"]` for `NotResource` with a `TODO` in the source; no combinatorial
   coverage is claimed for either element.
3. **Policy variables** (e.g. `${aws:username}`), account/partition wildcards
   inside ARNs, and regex-shaped resource segments are outside the finite
   domain.
4. **Condition evaluation.** Parliament never evaluates whether a `Condition`
   is true for a request; it only distinguishes "has a Condition" from "has no
   Condition" when deciding Deny handling. Context-dependent authorization
   outcomes are out of scope.
5. **Non-S3 services and resource-level permission boundaries.** The unrelated
   SQS statement in C03 is a negative control only; no oracle is provided for
   SQS or other services.
6. **Malformed policies.** The suites only analyze policies that
   `Policy.is_valid` accepts; malformed-JSON and finding-location behavior is
   covered by the pre-existing unit tests instead.

## Audit caveat

The wrapper is an execution entry point, not an independent certification. An
audit should still read the implementation under
`parliament/policy.py` and `parliament/statement.py`, inspect real logs and
policies in context, and treat anything these synthetic fixtures cannot prove
as unknown rather than as verified behavior.
