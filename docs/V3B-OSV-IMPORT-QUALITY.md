# Stage V3b — OSV import and quality metrics

Status: bounded batch, offline

## Scope

V3b extends the V3a local SQLite database with explicit OSV payload import and quality reporting. It remains fixture/local-input only; it does not download or activate remote feeds.

Supported payloads:

- one OSV record as a dictionary or JSON string/bytes;
- a list of OSV records;
- an OSV batch object with `vulns`.

## Import behavior

- malformed JSON returns structured import errors;
- malformed records do not abort valid records in the same batch;
- per-record savepoints prevent partial advisory rows after a failed record;
- reimport by advisory ID is idempotent;
- OSV `schema_version`, `severity`, and `database_specific` fields are retained;
- V3a SQLite files are migrated in place with no advisory data loss.

## Quality metrics

`quality_metrics()` reports:

- database schema version;
- advisory count;
- affected package count;
- withdrawn advisory count;
- optional snapshot ID;
- optional source digest.

`record_snapshot(snapshot_id, source_digest)` stores the explicit local source provenance metadata. It does not imply that a remote source was fetched.

## Non-goals

- no network downloader;
- no feed updater;
- no remote URL opening;
- no automatic snapshot activation;
- no CVE/GHSA/NVD correlation;
- no CVSS/EPSS/KEV enrichment;
- no full ecosystem-specific version comparator;
- no vulnerability CLI or policy engine.
