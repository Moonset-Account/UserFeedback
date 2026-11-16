# Stage V3c — Snapshot activation and deterministic quality gate

Status: bounded batch, offline

## Scope

V3c adds local snapshot staging and activation over the V3a/V3b SQLite database. It does not fetch or activate any remote source.

## Manifest

`build_snapshot_manifest()` produces a canonical manifest containing:

- sorted advisory identities;
- aliases, OSV schema version, withdrawn state;
- sorted affected package identities;
- advisory and affected-package counts;
- deterministic `content_digest`.

The digest is independent of import order and is computed from canonical JSON.

## Lifecycle

Snapshots have explicit states:

```text
staged -> active -> retired
```

`stage_snapshot(snapshot_id, source_digest, manifest)` stores a local candidate. `activate_snapshot()` recomputes the current manifest and requires matching content digest and quality counts. On mismatch, activation raises `SnapshotActivationError` and leaves the candidate staged.

Only one snapshot can be active at a time. Activating a second verified snapshot retires the previous one.

## Safety boundary

- source digest must be explicitly supplied and prefixed with `sha256:`;
- digest presence is not proof of a remote download;
- no network, URL opening, feed updater, subprocess, or CLI is introduced;
- existing V3a/V3b import, matching, migration, and quality APIs remain available;
- activation does not mutate advisory content.

## Non-goals

- no remote snapshot downloader;
- no automatic activation after import;
- no full feed provenance chain;
- no policy decision or vulnerability CLI;
- no ecosystem-complete version semantics.
