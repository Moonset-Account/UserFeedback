# Electronic credential presentation planner

Given a wallet of already-verified credentials and a set of verification
branches ("conditions"), decide **which indivisible disclosure groups to
present** so that every condition is witnessed, while exposing as little as
possible and never mixing holders.

The planner is exact (no heuristics): it enumerates the problems the task
allows — at most 16 disclosure groups per wallet — in full.

---

## 1. Input format

A specification is a JSON object (or the equivalent Python mapping):

```json
{
  "attribute_weights": { "name": 1, "address": 4 },
  "credentials": [
    {
      "id": "c1",
      "issuer": "civil-registry",
      "holder": "alice",
      "verified": true,
      "groups": [
        { "id": "c1.g1", "attributes": ["name", "address"] }
      ]
    }
  ],
  "conditions": [
    {
      "id": "prove-name",
      "acceptable_issuers": ["civil-registry"],
      "alternatives": [
        ["name"],
        { "attributes": ["given-name", "family-name"],
          "acceptable_issuers": ["civil-registry", "passport-office"] }
      ],
      "same_credential": [["name", "birthdate"]]
    }
  ]
}
```

### Fields

| Field | Meaning |
| --- | --- |
| `credentials[].id` | Credential number. String, or an integer (normalised to text). **Must be unique.** |
| `credentials[].issuer` | Issuer name. |
| `credentials[].holder` | Holder identity. A presentation never mixes holders. |
| `credentials[].verified` | Defaults to `true`. Unverified credentials are never used. |
| `credentials[].groups[]` | `{ "id": ..., "attributes": [...] }`. A group is **indivisible**: you either disclose every attribute in it or none. Group ids must be unique across the whole wallet. |
| `conditions[].alternatives` | Substitutable ways to satisfy the branch. Each alternative is a list of attribute names, or an object `{ "attributes": [...] , "acceptable_issuers": [...] }`. A flat list of strings is shorthand for one alternative. |
| `conditions[].acceptable_issuers` | Branch-wide issuer allow-list. Intersected with any per-alternative list. |
| `conditions[].same_credential` | List of attribute sets that must be disclosed **by a single credential** — e.g. `[["name","birthdate"]]` means name and birthdate must both come from one credential. Shorthand `["name","birthdate"]` means one such set. |
| `attribute_weights` | Weight (privacy cost) per attribute. Non-negative numbers; missing attributes default to `1`. Exact rationals are preserved. |

Aliases accepted for convenience: `branches` for `conditions`, `weights` for
`attribute_weights`, `same_credential_groups` for `same_credential`. Supplying
both a name and its alias is a `SpecError` rather than an ambiguous input.

### Hard limit

**At most 16 disclosure groups** across the whole wallet. More raises
`SpecError`. This is what makes the exhaustive search affordable.

---

## 2. Semantics

1. **A condition is satisfied** when disclosure groups from **one holder**
   jointly cover all attributes of **some one alternative**, and every group used
   for that condition comes from an issuer accepted by both the alternative and
   the branch. If the alternative requires several attributes, they may be
   spread over several groups — but only *one* alternative needs to hold.
2. **Groups are indivisible.** Using a group to obtain `name` also exposes every
   other attribute in it. All of a presented group's attributes count towards
   the cost.
3. **The same group presented twice counts once.** The cost is computed over the
   *set* of groups in the presentation, never over how many conditions cite a
   group. Sharing is therefore free and encouraged.
4. **No mixing of holders.** The whole presentation comes from a single holder.
   The best holder is chosen by the same objective.
5. **Unverified credentials are ignored.**

### Objective (lexicographic)

1. **Least total weight** of the *set* of exposed attributes.
2. **Fewest credentials** used.
3. **Stable order**: the natural (numeric) ordering of credential numbers, then
   of disclosure group numbers. `c2` sorts before `c10`; `cred-2` before
   `cred-10`.
4. Fewer disclosure groups is a final refinement (it only matters when
   everything above has tied; this makes the order total and deterministic).

This ordering produces the behaviour the task calls out: when a condition is
cheapest on its own with one credential, but satisfying it *together* with
another condition would force an extra address disclosure, the planner picks
the combination with the **lower total cost for the whole presentation**, not
the sum of each condition's individual best.

---

## 3. Output

`Plan` (or `plan.to_json()` / `plan.as_dict()`):

* `satisfied` — whether a single-holder presentation exists.
* `holder` — the chosen holder.
* `selected[]` — the presented groups: `group_id`, `credential_id`, `issuer`,
  `holder`, `attributes`, `weight`.
* `exposed_attributes`, `total_weight`, `credential_ids`.
* `conditions[]` — **provenance for every condition**:
  * `alternative_index` and `required_attributes` — which alternative was used;
  * `groups` — the groups that witness it;
  * `disclosed_attributes` / `extra_attributes` — everything those groups expose,
    and the part not strictly required;
  * `standalone_groups` / `standalone_weight` / `standalone_credentials` — the
    cheapest way to satisfy *this condition alone*;
  * `follows_standalone` — whether the joint solution coincides with that
    standalone optimum;
  * `shared_groups` — groups also cited by another condition (counted once);
  * `reason` — a human-readable explanation.
* `unmet_conditions` and `notes` — diagnostics, including why a joint choice
  differs from a standalone optimum.

For unsatisfiable input the plan is returned with `satisfied=false` and
per-condition reasons (calling `require_plan` raises `UnsatisfiableError`
instead).

---

## 4. Public API

```python
from wallet_planner import (
    plan_presentation,   # mapping or WalletSpec -> Plan (never raises on unsat)
    plan_from_dict,      # explicit mapping wrapper
    require_plan,        # -> Plan, raises UnsatisfiableError if unsat
    parse_spec,          # mapping -> WalletSpec (raises SpecError)
    Plan, ConditionProvenance, GroupSelection,
    WalletSpec, Credential, DisclosureGroup, Condition, Alternative,
    SpecError, UnsatisfiableError,
    MAX_DISCLOSURE_GROUPS,  # 16
    natural_key,
)
```

`Plan` exposes `selected_group_ids`, `credential_count`, `group_count`,
`as_dict()` and `to_json()`.

### Command line

```
python3 -m wallet_planner SPEC.json           # plan as JSON
python3 -m wallet_planner --strict SPEC.json  # exit 1 when unsatisfiable
python3 -m wallet_planner -                   # read the spec from stdin
```

Exit codes: `0` success, `1` unsatisfiable with `--strict`, `2` bad input.

---

## 5. Running and verifying

Requires Python 3.11.6, standard library only. Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_planner.py` covers the required behaviours, `tests/test_cli.py` the
CLI, and `tests/test_differential.py` compares the planner against an
independent brute-force oracle (`tests/oracle.py`) on 500 random
specifications.

---

## 6. What is verified

Measured by actually running the suite (58 tests, all passing):

* **Minimal extra disclosure** — bundled vs. split groups, weight-vs-count
  ordering, zero and fractional weights.
* **Fewest credentials, then stable numbering** — including natural ordering
  (`c9 < c10`, `cred-2 < cred-10`) and group numbering within a credential.
* **Indivisible groups** — a group's extra attributes are exposed and costed;
  attribute output order is canonical regardless of input order.
* **Same group twice counts once** — repeated citations cost once; the shared
  group is reported in `shared_groups`.
* **Joint vs. standalone** — the prompt's address scenario (choosing the single
  bundling group at weight 2 over two groups at weight 3) plus several
  variants, each asserted against `standalone_groups`/`follows_standalone` and
  the `reason` text.
* **Issuers** — branch-level, alternative-level, their intersection, and the
  rule that one alternative cannot span issuers.
* **Same-credential constraints** — satisfied by a single credential, reported
  as unsatisfiable when no credential holds the attributes, and scoped to the
  named attributes only.
* **No holder mixing** — mixed-holder specs are unsatisfiable; the best single
  holder is chosen on objective.
* **Provenance** — every condition reports its alternative, groups, disclosed
  and extra attributes, and standalone comparison.
* **Validation** — >16 groups rejected, exactly 16 accepted, duplicate ids,
  negative/boolean weights, ambiguous aliases, empty alternatives.
* **Exactness** — `test_differential.py` cross-checks 500 random specs against
  a from-scratch brute-force oracle; an additional local run of 800 wider
  random specs (group budget 10) produced **0 mismatches**.
* **Performance** — worst-case 16-group specifications used in local timing runs
  complete in roughly 0.3–1.3 s.

Not verified: behaviour under concurrent use, memory ceilings for adversarial
16-group wallets with pathologically many alternatives, or credential formats
beyond the JSON model described above. The 800-case wider differential run and
the timing figures were executed locally during development, not inside the
committed test suite.
