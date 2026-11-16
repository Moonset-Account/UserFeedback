# V5q — Non-destructive advisory reimport

## Contract

Reimporting an existing advisory updates the advisory row in place and must not delete related:

- source records;
- NVD raw or normalized enrichment;
- KEV records;
- active advisory relations.

Identical NVD and KEV enrichment imports are idempotent and report zero newly imported records.

## Non-goals

This remediation does not yet change NVD revision selection, CPE grammar validation, KEV date validation, or quality-gate thresholds. Those remain separate audit findings.
