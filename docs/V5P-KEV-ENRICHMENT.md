# V5p — Offline CISA KEV enrichment

The importer accepts local CISA KEV JSON fixtures or catalog batches.

Contract:

- required KEV fields are validated;
- CVE IDs are exact-matched to existing advisory aliases;
- unknown and ambiguous CVEs are rejected;
- records are stored separately with `source=cisa-kev` and a content digest;
- KEV does not alter CVSS, severity, advisory fingerprints or risk decisions;
- no network access is performed.
