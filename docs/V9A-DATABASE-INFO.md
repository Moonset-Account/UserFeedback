# V9a — read-only database-info

`vuln-db database-info` reports metadata for the verified active snapshot:

```bash
python -m src vuln-db database-info --active SNAPSHOT_ROOT/active
```

The command is read-only, verifies the active snapshot before reporting it, returns the snapshot/source/content/manifest digests and quality/count metadata, and never returns advisory payloads. It fails closed when the active pointer or snapshot verification is invalid.
