# V8f — explicit snapshot rollback

V8f adds `rollback_versioned_snapshot(active_pointer, target_snapshot)`.

The rollback contract is fail-closed:

1. require an existing active symlink;
2. verify the current active snapshot completely;
3. verify the requested target snapshot completely;
4. reject a target that is already active;
5. create a same-directory temporary symlink;
6. atomically replace the active pointer with `os.replace`;
7. fsync the parent directory on a best-effort basis;
8. return both target and previous snapshot IDs.

A malformed or tampered target cannot change the active pointer. Versioned snapshot directories are retained; V8f does not delete snapshots, implement retention, or add a CLI. The staged SQLite files remain unchanged and continue to report `staged` when read back directly.
