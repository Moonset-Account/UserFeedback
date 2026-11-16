# V8h — metadata-only snapshot reconciliation

V8h adds a read-only reconciliation report for the versioned vulnerability snapshot store.

The Python API is `build_reconciliation_report(root, active_pointer)`. It verifies snapshot manifests without reading source-record payloads and returns deterministic metadata:

- active snapshot ID;
- valid and invalid snapshot counts;
- source counts;
- snapshot metadata and manifest digests;
- structured issues;
- canonical report SHA-256.

The report never activates, rolls back, prunes, downloads, or mutates a snapshot.

The explicit CLI command is:

```bash
python -m src vuln-db reconcile --root SNAPSHOT_ROOT --active ACTIVE_POINTER
```

A healthy report exits with status 0. Any reconciliation issue exits with status 3 while still emitting the metadata report. `--output PATH` writes it through the existing private atomic writer.

This is a reconciliation/diagnostic boundary, not a full database updater and not a claim of complete source coverage.
