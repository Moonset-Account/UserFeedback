# V8g — safe snapshot retention and pruning

V8g adds `prune_versioned_snapshots()` for an explicit retention policy.

The operation is fail-closed:

- the snapshot root must be a regular directory;
- the active pointer must exist and point to a fully verified snapshot;
- every non-hidden directory in the root is verified before any deletion;
- malformed, tampered, duplicate, or unexpected symlink entries block the operation;
- unknown `keep_snapshot_ids` block the operation;
- the active snapshot is always protected;
- explicitly retained rollback targets are always protected.

`apply=False` is the default and returns a deterministic dry-run report. With `apply=True`, deletable directories are first atomically moved into a same-root temporary trash directory using `os.replace`. Only after every move succeeds is the trash removed. If a move fails, already moved directories are restored before the error is returned.

V8g does not select retention targets automatically, delete the active snapshot, delete declared rollback targets, implement a scheduler, or add a CLI.
