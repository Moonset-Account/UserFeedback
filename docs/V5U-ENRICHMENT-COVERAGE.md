# V5u — Truthful enrichment coverage

`VulnerabilityDatabase.enrichment_coverage_report()` is an additive, read-only report for requested enrichment sources.

Supported sources:

- `nvd`
- `kev`

Each source is classified as exactly one of:

- `available`: requested and at least one active record exists;
- `unavailable`: requested but no record exists;
- `not_requested`: intentionally outside the current request scope.

The report includes per-source `record_count`, requested source order, aggregate status counters, and a deterministic `content_digest`. Existing NVD/KEV read APIs and their error behavior remain unchanged.
