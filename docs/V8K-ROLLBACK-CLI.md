# V8k — explicit snapshot rollback CLI

V8k exposes the existing atomic rollback API through an explicit CLI command.

A rollback must include `--apply`:

```bash
python -m src vuln-db rollback \
  --active ACTIVE_POINTER \
  --target SNAPSHOT_DIR \
  --apply
```

Without `--apply`, the command exits with status 3 and does not inspect or mutate the active pointer. With `--apply`, the target is verified first and the active symlink is replaced atomically. The report includes the new snapshot ID and the previous active snapshot ID.

The command rejects a same-target rollback, malformed/tampered targets, missing active pointers, and any failed verification without partial switching. It does not download, import, prune, run package managers, or execute analyzed-repository code.
