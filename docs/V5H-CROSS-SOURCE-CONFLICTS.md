# V5h — Cross-source conflict diagnostics

## Scope

This bounded batch diagnoses disagreements inside exact-alias reconciliation groups. It does not choose a winner and does not write merge decisions.

## Conflict fields

The report compares source-backed signatures for:

- withdrawn/status;
- summary/details content;
- severity metadata;
- affected package/range assertions;
- references.

Values are represented by deterministic digests or compact metadata, not full advisory prose.

## Policy

A field is a conflict when more than one canonical signature exists in a reconciliation group. The report retains advisory IDs, source IDs, field name, and signatures. It never resolves the conflict.

## Non-goals

- source priority;
- newest-record wins;
- severity normalization;
- merge/split application;
- network access.
