# Changelog

All notable changes to `coderisktools-scanner` are documented here.

## [Unreleased]

### Vulnerability enrichment

- versioned the local vulnerability database schema with explicit initialization, migration, current-state reporting and fail-closed future-version handling;
- preserved NVD configuration logic instead of flattening it: node `AND`/`OR`, `negate`, nested children and a legacy flat CPE projection are all available;
- preserved NVD references, source tags and CVE change history through parser, SQLite enrichment and normalized reports;
- added deterministic readback tests and documentation for the V5x–V5z contracts.

### Scanner evidence boundary

- documented the required distinction between runtime remediation evidence and publication-safe redaction;
- runtime evidence preservation remains an open formatter/pipeline audit finding; checked-in/public artifacts remain synthetic or explicitly redacted.

## [3.0.1] — 2026-07-20

### Changed

- refreshed the public package release metadata for the current Scanner flagship;
- updated the documented registry inventory to 299 native, 267 line and 32 contextual detectors;
- updated CI/CD coverage documentation to 73 policy detectors;
- synchronized install examples for pre-commit and GitHub Actions with `v3.0.1`.

### Security posture

- local-first and offline by default;
- no runtime telemetry;
- no runtime dependencies;
- bounded input handling;
- redacted output by default;
- no execution of target-project code;
- synthetic fixtures only in the public repository.

### Limitations

A clean result is not proof that code is secure. Findings can include false positives and false negatives. This release is not a security audit, certification, compliance guarantee or legal opinion.

## [3.0.0] — 2026-07-16

First public GitHub release of the MIT-licensed CodeRiskTools Secret Scanner Engine.

See the GitHub release notes and attached provenance files for the original 3.0.0 artifact record.
