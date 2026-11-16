# V5v — Explicit enrichment quality thresholds

`VulnerabilityDatabase.quality_threshold_report()` evaluates explicit local quality gates without changing the legacy `quality_metrics_report()` contract.

Supported thresholds:

- `min_advisories`;
- `min_unique_cves`;
- `required_enrichment_sources` (`nvd`, `kev`).

Each check is classified as:

- `pass` — observed value satisfies the threshold;
- `fail` — data is evaluable but below the threshold;
- `not_evaluable` — no advisory data exists to evaluate the check.

An overall `not_evaluable` state never becomes `pass`; it returns `quality_status: failed`. Threshold types and source names are validated, and the canonical report includes a deterministic digest.
