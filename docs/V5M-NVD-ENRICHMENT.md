# V5m — Offline NVD enrichment

## Scope

This stage parses local NVD CVE API 2.0 fixtures and stores CVSS, CWE, CPE match, status, dates and descriptions as separate enrichment records.

## Contract

- NVD records require a valid CVE ID;
- enrichment attaches only to one exact existing advisory alias;
- ambiguous or unknown CVEs are rejected;
- source payloads receive a content digest;
- existing advisory fields and fingerprints are not overwritten;
- no network access is performed.

## Non-goals

- CPE-to-PURL guessing;
- replacing OSV/CNA values;
- CVSS selection policy;
- live NVD downloading;
- automatic component matching.
