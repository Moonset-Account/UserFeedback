# V8e — atomic staged-snapshot promotion

V8e promotes a verified staged snapshot by atomically replacing an `active` symlink. The SQLite database is not copied or modified during promotion.

`promote_versioned_snapshot(staged_path, active_pointer)`:

1. runs the complete V8d readback on the staged directory;
2. rejects an active destination that is a regular file/directory;
3. verifies the current active target, if present;
4. creates a same-directory temporary symlink;
5. atomically replaces the active pointer with `os.replace`;
6. best-effort `fsync`s the parent directory;
7. returns the new and previous snapshot IDs.

A failed staged verification leaves the existing active pointer unchanged. The previous target remains available at its original versioned path, which provides the data needed for a later explicit rollback batch. V8e does not delete old snapshots, implement retention, or provide a rollback command.
