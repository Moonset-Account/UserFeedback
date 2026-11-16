# V5p — Offline CISA KEV enrichment

The importer accepts local CISA KEV JSON fixtures or catalog batches.

Contract:

- required KEV fields are validated;
- `dateAdded` and `dueDate` must be strict `YYYY-MM-DD` strings that name real
  calendar dates (for example `2025-02-29` is rejected); a bad date rejects only
  that record with a locatable `record N` error, while other batch records still
  import;
- a payload whose JSON cannot be decoded at all is a file-level failure: the
  receipt reports a single `payload:` error with zero records seen and zero
  imports, instead of record-level errors;
- `knownRansomwareCampaignUse` accepts historical boolean flags and the
  catalog's `Known`/`Unknown`/`Yes`/`No` string flags; null/missing stays null,
  and any other type rejects the record without interrupting later records;
- CVE IDs are exact-matched to existing advisory aliases;
- unknown and ambiguous CVEs are rejected;
- records are stored separately with `source=cisa-kev` and a content digest;
- KEV does not alter CVSS, severity, advisory fingerprints or risk decisions;
- no network access is performed.
