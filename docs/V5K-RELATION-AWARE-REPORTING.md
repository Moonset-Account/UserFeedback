# V5k — Relation-aware advisory reporting

## Scope

This stage exposes active and historical advisory relations as a deterministic read-only report.

## Contract

- active relations are returned by default;
- inactive relations are available with `active_only=False`;
- each relation includes decision ID, relation type, advisory IDs, exact alias evidence and source IDs;
- source advisory rows remain separate and unchanged;
- the report has a canonical content digest.

## Non-goals

- merging advisory content;
- hiding source provenance;
- applying new decisions;
- network access.
