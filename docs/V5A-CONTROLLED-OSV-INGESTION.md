# Stage V5a — Controlled local OSV ingestion

Status: bounded batch, local-file-only, no network

## Command

```bash
secret-scanner osv-import \
  --input ./osv-feed.json \
  --db ./vulnerability.sqlite \
  --snapshot-id osv-2025-01 \
  --source-id osv-fixture
```

The command stages a snapshot. Activation is explicit:

```bash
--activate
```

No URL input, downloader, HTTP client, or implicit source discovery exists in this stage.

## Accepted input

The bounded local JSON reader accepts:

- one OSV advisory object;
- an array of advisory objects;
- an object with a `vulns` advisory array.

The source is read as a regular non-symlink file under a fixed byte limit. Its exact raw bytes produce:

```text
source_digest = sha256:<digest>
```

## Two-phase safety

1. read, hash, and parse the local file;
2. import all records into a temporary validation database;
3. reject the entire feed if any record fails;
4. import the validated records into the target database;
5. build a deterministic manifest with source provenance;
6. stage the snapshot;
7. activate only when `--activate` was explicitly supplied.

A feed containing one malformed record does not create a staged snapshot and does not mutate the target database.

## Reconciliation report

The command emits JSON containing:

- source path/id/digest;
- snapshot ID/state;
- activation status;
- records seen;
- advisories imported;
- affected packages imported;
- error list.

Source provenance is retained in the snapshot manifest as `source_id`, `source_path`, `source_digest`, and ingestion schema.

## Non-goals

- no remote URLs;
- no feed scheduling;
- no automatic activation;
- no signature verification of external feeds;
- no delta/update protocol;
- no claim that a local fixture is an official current OSV release.
