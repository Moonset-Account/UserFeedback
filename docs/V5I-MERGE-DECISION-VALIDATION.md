# V5i — Merge-decision validation gate

## Scope

This bounded batch validates an already recorded merge/split decision before any future application step.

## Statuses

- `valid`: all advisory IDs exist and share at least one exact alias;
- `invalid`: all IDs exist but no exact alias evidence connects them;
- `stale`: one or more advisory IDs no longer exist.

The result includes shared alias evidence and conflict types from the V5h report. Existing conflicts are evidence for operator review, not an automatic rejection.

## Non-goals

- applying merge/split;
- deleting or rewriting advisory records;
- changing fingerprints;
- resolving source conflicts;
- creating decisions automatically.
