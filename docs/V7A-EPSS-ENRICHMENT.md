# V7a — offline FIRST EPSS enrichment

V7a adds explicit offline ingestion for supplied FIRST EPSS records.

Supported record shape:

- `cve`;
- `epss` probability in `[0, 1]`;
- `percentile` in `[0, 1]`;
- ISO calendar `date`.

The importer requires an exact existing advisory correlation, stores a source/revision digest in SQLite, and exposes:

- `epss_record(cve_id)`;
- additive `exploitation_intelligence_report(cve_id)` with EPSS and KEV sections.

The schema advances from version 2 to version 3. Existing databases migrate without replacing existing rows. EPSS is enrichment metadata only: it does not affect advisory, component, vulnerability-match, or snapshot identity.

This is offline supplied-record ingestion, not a downloader or automatically updated EPSS feed.
