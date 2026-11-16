# V8m — read-only source status

`vuln-db source-status` reports the health of sources represented by the local verified snapshot store:

```bash
python -m src vuln-db source-status \
  --root SNAPSHOT_ROOT \
  --active ACTIVE_POINTER
```

The output is derived from the deterministic reconciliation report and contains only source IDs, verified snapshot counts, snapshot IDs, active-source state, and reconciliation issues. It does not read source record payloads, make network requests, activate snapshots, prune snapshots, or modify files.

A reconciliation issue keeps the command fail-closed with exit status 3. The report includes a digest so repeated reads of unchanged storage can be compared byte-for-byte.
