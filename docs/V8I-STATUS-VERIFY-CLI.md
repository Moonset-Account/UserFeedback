# V8i — read-only snapshot status and verification CLI

V8i extends the explicit local vulnerability database CLI with two read-only commands.

Verify one snapshot:

```bash
python -m src vuln-db verify --snapshot SNAPSHOT_DIR
```

This delegates to `verify_versioned_snapshot()`, emits only verified metadata, and exits with status 3 on malformed or tampered snapshots. It never repairs or activates the snapshot.

Show store status:

```bash
python -m src vuln-db status --root SNAPSHOT_ROOT --active ACTIVE_POINTER
```

Status uses the deterministic V8h reconciliation report and preserves its fail-closed behavior. It does not mutate the active pointer or any snapshot.

This batch does not add downloading, importing, building, activation, rollback, pruning, or package-manager execution.
