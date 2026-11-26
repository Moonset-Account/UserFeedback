# Audit retention issuance (`retention`)

When the archive shrank its retention volume, several already-filed incidents
still had to keep records whose combination restores what the operations
looked like at the time. This module takes each log's **original source**,
**timestamp**, **occupancy** and **event relations**, and issues the evidence
combination to retain per case (by default: one initiation, one independent
approval and one actual effect).

One original record may serve several cases, and several receipts copied from
the same original count as **one** source (charged once, not per copy).

## Public API

```python
from retention import (
    LogRecord, CaseSpec, EvidenceLink, EvidenceRequirement,
    RetentionRequest, build_plan, DEFAULT_REQUIREMENTS,
    RetentionInputError, RetentionCapacityError,
)
```

### Inputs

`LogRecord(log_id, source_id, timestamp, size, copied_from=None)`

| field | meaning |
| --- | --- |
| `log_id` | unique id of this concrete log/receipt |
| `source_id` | id of the original record; copies share it and are charged once |
| `timestamp` | int/float epoch-style time (must be finite) |
| `size` | occupancy of the log, a non-negative integer |
| `copied_from` | optional id of the log this receipt was copied from; it must belong to the same `source_id` |

`CaseSpec(case_id, tier, requirements=DEFAULT_REQUIREMENTS)` — a filed
incident. `tier` is its priority (higher is handled first). Each requirement is
an `EvidenceRequirement`:

```python
EvidenceRequirement(role, after=None, strict_after=False, distinct_source_from=None)
```

* `after` — this role must be at or after the referenced role in time
  (strictly after when `strict_after=True`).
* `distinct_source_from` — the chosen log's original source must differ from
  the referenced role's source (this is how an approval is kept independent
  from the initiation, even when a receipt copy of the original is offered).

`DEFAULT_REQUIREMENTS` is exactly:

```python
EvidenceRequirement("initiation")
EvidenceRequirement("approval", after="initiation", distinct_source_from="initiation")
EvidenceRequirement("effect",   after="approval")
```

`EvidenceLink(case_id, role, log_id)` — the event relation "this log can serve
as this role for this case". Duplicate relations are collapsed.

`RetentionRequest(logs, cases, links, budget, source_costs=None,
max_search_nodes=200_000, max_combo_nodes=500_000)` — one issuance request.
`source_costs` optionally overrides the per-source charge; by default a source
is charged the **largest** `size` among its members (conservative and
deterministic).

### Output — `build_plan(request) -> RetentionPlan`

| field | meaning |
| --- | --- |
| `satisfied_case_ids` | cases that received a retained evidence combination |
| `cases` | one `CaseOutcome` per case (`satisfied`, `evidence`, `unmet`) |
| `sources` | retained sources, each with `size` and its `log_ids`, plus `kept_log_ids` / `dropped_copy_ids` |
| `logs` | concrete logs the archive task should keep (evidence logs + originals of chosen receipts) |
| `droppable_copies` | receipt copies of a retained source that are not needed |
| `unmet_evidence` | every evidence item not retained, with a reason code |
| `total_size`, `budget`, `remaining_budget` | retained charge versus budget |
| `satisfied_by_tier` | `((tier, count), ...)` highest tier first |
| `optimal`, `search_nodes` | whether exact optimality was proven |

`UnmetEvidence.reason` is one of:

* `no_candidate` — no log is linked as this role;
* `no_feasible_combination` — candidates exist but ordering/independence leave
  no valid combination for the case;
* `budget_insufficient` — a valid combination exists but does not fit the
  optimal plan's remaining budget;
* `not_retained` — valid, but the solver hit its search limit, so optimality
  is not proven (`plan.optimal is False`).

## Objective

Optimisation is lexicographic:

1. maximise provable cases in the highest priority tier,
2. then the next tier, and so on,
3. then minimise total retained occupancy,
4. then a deterministic lexicographic tie-break over the evidence.

A source selected for one case is free to every other case.

## Running

Requires Python 3.11 (no third-party dependencies).

```bash
python3 -m unittest discover -s tests -v
```

## Verified scope

The test suite (`tests/test_retention.py`) uses local, synthetic data only. It
is asserted against real execution and covers:

* the default three-item combination and archive-task output;
* zero cases, zero budget, zero-size sources;
* receipt copies: one charge per source, receipt-original resolution, invalid
  cross-source receipts, copy cycles, droppable copies;
* independence: an approval that is a copy of the initiation's source is
  rejected; separate sources satisfy the case;
* ordering: approval not before initiation, effect not before approval, equal
  timestamps for non-strict `after`, strict `after`, requirement cycles;
* one source serving several cases (incl. sharing that only fits within
  budget because it is charged once);
* budget optimisation: maximising case count, tier priority beating case
  count, tier maximisation followed by the cheaper tie-break;
* every `unmet_evidence` reason code, including the pruned-search case;
* input validation (duplicate ids/roles, unknown references, bad budgets,
  non-finite timestamps, requirement/copy cycles, combo-node limit);
* source-cost override and default-largest-member charging;
* determinism under shuffled log/link order;
* cross-checks against an independent exhaustive brute-force oracle on small
  scenarios, plus 40 randomised scenarios asserting structural invariants.

The solver is exact for the state space it explores; when `max_search_nodes`
or `max_combo_nodes` is exceeded it either raises (`RetentionCapacityError`
for combination enumeration) or returns a best-effort plan with
`optimal=False` (for the cross-case search).
