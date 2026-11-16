# V5n — Normalized NVD provenance

NVD raw records from V5m are projected into separate tables for CVSS metrics, weaknesses and CPE matches.

Contract:

- every normalized row has `source=nvd`;
- every row has a content-derived digest;
- raw NVD enrichment remains available;
- advisory data and fingerprints are not overwritten;
- readback is deterministic.
