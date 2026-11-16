# V5g — Exact cross-source reconciliation report

## Scope

This bounded batch creates a deterministic reconciliation report from already imported advisory rows and the exact alias index.

## Evidence

A group may contain multiple advisory records only when they share an exact normalized alias. The report includes the alias evidence, advisory IDs, source IDs, source-record revision counts, and ambiguity classification.

## Classification

- `single-source`: all advisory records in the group come from one source;
- `cross-source`: at least two source IDs are present;
- `ambiguous=true`: more than one advisory ID is connected by exact alias evidence.

`cross-source` and `ambiguous` are independent properties. A cross-source group is not a merge decision.

## Determinism

Groups, advisory IDs, source IDs, and evidence aliases are sorted. The report contains a canonical SHA-256 content digest.

## Non-goals

- changing advisory rows;
- applying merge/split decisions;
- description similarity;
- package/name similarity;
- dates, CVSS, or severity heuristics;
- network access.
