# V6b — CVSS-independent vulnerability fingerprint

The vulnerability match fingerprint is derived from the advisory identity, component identity, and dependency occurrence identity. CVSS metrics and NVD enrichment revision metadata are intentionally excluded.

The acceptance regression imports the same advisory with an initial NVD CVSS record and then with changed CVSS score and `lastModified`. The real `match_component()` fingerprint remains byte-identical across both revisions.

This batch required no production-code change: the invariant was already implemented by `vulnerability_fingerprint()` and is now protected by an end-to-end test.
