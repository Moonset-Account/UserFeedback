# V5j — Apply approved merge/split decisions

## Scope

This bounded batch applies a previously recorded and validated merge/split decision as an auditable relation. It does not rewrite advisory rows or source records.

## Contract

- `apply_merge_decision(decision_id)` requires V5i `valid` status;
- the first application creates one active relation row;
- repeated application is idempotent;
- `rollback_merge_decision(decision_id)` deactivates the relation without deleting the ledger decision;
- stale, invalid, or missing decisions are rejected without mutation.

## Non-goals

- selecting a preferred advisory;
- copying descriptions, severity, ranges, or provenance;
- deleting duplicate advisories;
- changing fingerprints;
- automatic correlation;
- network access.
