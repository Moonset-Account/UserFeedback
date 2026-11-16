# V8d — isolated versioned snapshot staging

V8d builds a new vulnerability snapshot in an isolated SQLite database and publishes it as a staged directory. It does not mutate an existing database and does not activate the staged snapshot.

A staged snapshot directory contains:

- `snapshot.sqlite3` — imported and integrity-checked local database;
- `manifest.json` — deterministic source/snapshot metadata and manifest digest.

The build contract:

1. validate source/snapshot IDs and source SHA-256;
2. validate bounded JSON and normalize an advisory object, array, or `vulns` array;
3. import records into a new temporary SQLite database;
4. require all records to import successfully;
5. require `PRAGMA integrity_check == ok`;
6. build the deterministic database snapshot manifest;
7. record database snapshot state as `staged`;
8. hash and write the manifest;
9. atomically publish the temporary directory.

`verify_versioned_snapshot()` rechecks the manifest hash, SQLite integrity, content digest/counts, staged state, and absence of an active snapshot in the isolated database.

The destination must not already exist. This prevents accidental replacement of an existing staged snapshot. Activation, promotion, rollback, retention, and CLI remain outside V8d.
