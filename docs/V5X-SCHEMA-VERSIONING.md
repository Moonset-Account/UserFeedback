# V5x — Explicit database schema versioning

`VulnerabilityDatabase` now exposes `schema_status_report()` and treats schema compatibility as an explicit contract.

Current schema:

```text
2
```

Lifecycle statuses:

- `initialized` — a new database was created at the current schema;
- `migrated` — a supported older schema was upgraded to the current schema;
- `current` — an existing database already uses the current schema.

Future schema versions fail closed before overwriting the stored metadata. Invalid schema-version values also fail closed. Legacy migrations preserve `migrated_from` metadata, and the status report contains a deterministic canonical digest.
