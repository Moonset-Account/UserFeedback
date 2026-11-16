# Stage V3a — Mini lokalna baza OSV

Status: bounded batch, fixtures-only, offline

## Scope

V3a introduces a dependency-free SQLite database populated by explicitly supplied OSV-shaped records. It does not download feeds and does not change the existing secret scanner contracts.

Implemented public API:

- `VulnerabilityDatabase(path)`;
- `import_osv_records(records)`;
- `match_component(component)`;
- `explain_match(fingerprint)`;
- `integrity_check()`;
- `advisory_count()`.

## Schema

The mini schema contains:

- `metadata`;
- `advisories`;
- `affected_packages`;
- `affected_ranges`;
- `affected_versions`;
- `advisory_references`;
- `matches`.

Foreign keys are enabled and the database runs SQLite integrity checks.

## Matching

V3a supports:

- ecosystem/package identity matching;
- exact affected versions;
- OSV `ECOSYSTEM`-style introduced/fixed/last_affected events;
- fixed-version explanation;
- withdrawn advisory filtering by default;
- `high` confidence for exact package identity plus exact component version;
- deterministic vulnerability occurrence fingerprints;
- stored explanation lookup by fingerprint.

The bounded version comparator is suitable for numeric fixture versions. It is not yet the complete comparator framework required for all ecosystems.

## Safety and provenance

- Import is explicit and local-only.
- No network calls are made.
- No subprocesses or package managers are used.
- OSV identifiers, aliases, references, published/modified/withdrawn values, and source `osv` are retained.
- A malformed record is counted as an import error rather than silently accepted.
- Existing `Finding`, `ScanResult`, baseline, SARIF, and secret CLI contracts are untouched.

## Non-goals

- no full OSV feed downloader;
- no updater or snapshot activation;
- no SQLite migrations/rollback pipeline;
- no CVE/GHSA/NVD correlation;
- no CVSS/EPSS/KEV enrichment;
- no vulnerability CLI;
- no policy engine;
- no production database claim.
