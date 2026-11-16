# V8l — offline update orchestration

V8l adds one explicit, offline update boundary:

```bash
python -m src vuln-db update \
  --input osv.json \
  --root SNAPSHOT_ROOT \
  --source-id osv \
  --snapshot-id snapshot-id
```

The input must be a local regular file, never a URL or symlink. The command reads the file, derives its SHA-256 digest, imports the payload through the existing isolated snapshot staging path, and verifies the resulting snapshot. It does not open network connections.

Staging is the default. Activation is a separate explicit operation in the same command and requires both `--active ACTIVE_POINTER` and `--apply`:

```bash
python -m src vuln-db update \
  --input osv.json \
  --root SNAPSHOT_ROOT \
  --source-id osv \
  --snapshot-id snapshot-id \
  --active ACTIVE_POINTER \
  --apply
```

Failures leave the existing active pointer unchanged. Existing `osv-import`, `verify`, `status`, `list-snapshots`, `prune`, and `rollback` contracts remain unchanged. This batch does not implement network downloading, incremental cursors, source quality metrics, or multi-source correlation.
