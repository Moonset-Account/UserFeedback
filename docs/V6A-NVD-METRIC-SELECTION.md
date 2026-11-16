# V6a — deterministic NVD CVSS metric selection

NVD enrichment preserves every imported CVSS metric in `nvd_normalized_report()["cvss"]` and adds the additive `preferred_cvss` field for presentation.

Selection order is deterministic:

1. highest supported CVSS version (`4.0`, `3.1`, `3.0`, `2.0`; the existing parser representation `40`, `31`, `30`, `20` is retained);
2. `Primary` before `Secondary` for the same version;
3. higher numeric `baseScore` for otherwise equivalent candidates;
4. canonical metric bytes as a final tie-breaker.

CVSS changes affect enrichment revision/report content, but not the advisory snapshot manifest identity. No legacy `cvss` entries are discarded.
