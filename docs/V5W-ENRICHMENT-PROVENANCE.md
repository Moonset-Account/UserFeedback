# V5w — Catalog-level NVD/KEV provenance

NVD and CISA KEV imports now write to the existing canonical `source_records` ledger.

Each imported enrichment record has:

- source ID (`nvd` or `kev`);
- native record ID (CVE);
- stable source-record fingerprint;
- canonical content digest;
- linked advisory ID.

The importer writes provenance inside the enclosing record savepoint (`commit=False`), preserving all-or-nothing rollback semantics. Direct `record_source_record()` calls retain their previous committed behavior.

`enrichment_provenance_report(cve_id, sources=...)` is additive and read-only. It returns only provenance metadata, not raw `record_json`, with deterministic ordering, per-source counters, schema version, and canonical report digest. Unknown sources are rejected. Duplicate NVD/KEV imports do not create duplicate provenance rows.
