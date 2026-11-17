# Difference evidence: resource-derivation contract tests

This note records what was observed against the unmodified implementation while
building the C01-C04 combinatorial tests, and what was deliberately **not**
promised. It is evidence about current behavior; it does not change any
production code.

## Scope of the change

```
?? tests/support/__init__.py
?? tests/support/resource_contracts.py
?? tests/unit/test_resource_derivation_c01.py
?? tests/unit/test_resource_derivation_c02.py
?? tests/unit/test_resource_derivation_c03.py
?? tests/unit/test_resource_derivation_c04.py
?? tools/local_verify.py
?? tools/README.md
?? tools/DIFF_EVIDENCE.md
```

`git status parliament/` reports a clean tree: no file under `parliament/` was
edited, and no test was made to pass by weakening or altering the
implementation. All new code is tests, fixtures, the replay entry point, and
documentation.

## Behaviors confirmed against the real implementation

Each row below was reproduced through `analyze_policy_string` plus
`get_allowed_resources` / `get_resources_for_privilege` and is locked by a
named test. Placeholder resources only; no real account data.

| Observation | Consequence pinned by tests |
| ----------- | --------------------------- |
| Unconditional `Deny` and `Allow` naming the same concrete object ARN -> the object is excluded while a sibling object stays allowed. | C01 `test_same_resource_unconditional_deny_overrides_allow`; independent oracle agreement on 8 seeded policies. |
| Unconditional `Deny` on `"*"` -> `get_allowed_resources` returns `[]` even with concrete Allows, in either statement order. | C02 `test_unconditional_star_deny_empties_all_concrete_allows`, `test_star_deny_clears_even_when_deny_is_first_or_last`. |
| `Deny` on `"*"` **with** a `Condition` (`Bool/aws:MultiFactorAuthPresent`) -> concrete Allows survive; same for a conditional Deny on a concrete ARN. | C02 conditional-deny tests. |
| `get_references` still lists the conditional Deny statement under the resource; Condition-aware skipping happens only inside `get_allowed_resources`. | C02 `test_references_still_report_conditional_deny_statement`. |
| Full permutation of statement order leaves the allowed set unchanged, and each Sid keeps its own `get_resources_for_privilege` output (no cross-statement bleed). | C03 `test_all_permutations_keep_the_same_allowed_set`, `test_grfp_results_are_attached_to_the_correct_statement`. |
| Scalar and single-element-list `Action`/`Resource` derive identical results. | C03 scalar-vs-list tests. |
| Adding an unrelated `sqs:SendMessage` statement or a bucket ARN (`arn:aws:s3:::b-alpha`, no slash) does not change `s3:GetObject` results; bucket ARN never appears in object-privilege derivation. | C03 unrelated-resource tests. |
| `s3:GetObject`, `s3:Get*`, `s3:*`, and `"*"` all reach the same object-resource derivation when paired with an object ARN; `s3:ListBucket` yields no resources for `GetObject`. | C03 action-form tests. |
| The seven-statement failure sample reduces deterministically to the 1-minimal pair `[BroadAllow, TargetDeny]`; object ARNs never turn into bucket ARNs or `"*"`, and surviving Sids remain a subsequence of the original order. | C04 `TestFailureSampleReduction`; the reducer asserts 1-minimality itself. |

## Boundary where a "difference" exists versus a full IAM authorizer

The library source itself flags this in `Policy.get_allowed_resources`:

> "I should do regex intersections across each resource, but this will avoid
> common situations for now"

Observed current behavior (characterized, not promised), C04
`TestArbitraryArnIntersectionIsNotPromised`:

- `Allow` on `arn:aws:s3:::b-alpha/*` plus unconditional `Deny` on
  `arn:aws:s3:::b-alpha/logs/a.txt` -> the glob Allow is still returned and the
  concrete ARN appears only as a separate, denied key. A real IAM evaluation
  could deny the concrete object covered by the glob; Parliament does not
  compute that overlap.
- The mirror image (`Allow` concrete, `Deny` glob) likewise keeps the concrete
  Allow.
- Only the bare `"*"` string gets the clear-to-empty special case; an
  ARN-shaped wildcard does not.

These tests assert what the code does today so a future change is visible; they
must not be read as a guarantee that this is correct IAM authorization
semantics.

## Other observed implementation details (documented, not relied on beyond tests)

- `Statement.get_resources_for_privilege` appends `"*"` twice for a `"*"`
  resource (resource-type loop plus the explicit `"*"` loop), e.g.
  `['*', '*']`. Tests compare as sets, consistent with downstream dict keys.
- An Allow statement using `s3:*` with only object ARNs can emit a
  `RESOURCE_MISMATCH` finding while remaining `is_valid`; the C03 wildcard
  action tests assert on `is_valid` and derived resources rather than on an
  empty finding set, so they do not over-constrain the linter.
- `get_allowed_resources` preserves first-seen resource ordering; tests compare
  sets because ordering is not part of the asserted contract.

## Verification runs

- `python -I tools/local_verify.py --case all` -> exit 0; 33 test methods,
  243 real assertions across C01-C04 (73/47/73/50), zero failures, zero skips.
- `python -m unittest discover -s tests/unit -p "test_*.py"` -> 97 tests, OK
  (64 pre-existing + 33 new), confirming no regression in the shipped suite.
- Negative controls (temporary, removed after the check) proved the wrapper
  exits non-zero for an injected assertion failure, for a zero-assertion case,
  and for an all-skipped case; the JSON record carried `status` values
  `failed` / `zero_assertions` respectively rather than a success message.
