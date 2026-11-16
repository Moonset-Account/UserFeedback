# V8j — list-snapshots and explicit prune CLI

V8j exposes the existing verified snapshot retention API through explicit local CLI commands.

List snapshot metadata:

```bash
python -m src vuln-db list-snapshots \
  --root SNAPSHOT_ROOT \
  --active ACTIVE_POINTER
```

Prune in dry-run mode (default):

```bash
python -m src vuln-db prune \
  --root SNAPSHOT_ROOT \
  --active ACTIVE_POINTER \
  --keep-snapshot-id SNAPSHOT_ID
```

Apply deletion only with an explicit flag:

```bash
python -m src vuln-db prune \
  --root SNAPSHOT_ROOT \
  --active ACTIVE_POINTER \
  --keep-snapshot-id SNAPSHOT_ID \
  --apply
```

The active snapshot and all explicitly retained rollback targets are protected. Every snapshot is verified before pruning; malformed stores fail closed and are not partially pruned. The command does not download, import, activate, rollback, run package managers, or execute analyzed-repository code.
