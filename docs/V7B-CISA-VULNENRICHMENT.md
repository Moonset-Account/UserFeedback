# V7b — offline CISA Vulnrichment

V7b adds offline ingestion of the CISA Authorized Data Publisher (`CISA-ADP`) container from CVE 5.x records.

The parser validates:

- `cveMetadata.cveId`;
- `containers.adp` as a non-empty list;
- presence of a `CISA-ADP` provider container;
- ADP metrics and SSVC `other.content` objects.

The database stores a revisioned, provenance-linked enrichment record and exposes:

- `vulnrichment_record(cve_id)` for normalized readback;
- additive `exploitation_intelligence_report(cve_id)` with `cisa_adp` and flattened `ssvc` projections.

The schema advances from version 3 to version 4. Existing advisory identity and dependent enrichment remain intact. Vulnrichment and SSVC values do not affect advisory, component, vulnerability-match, baseline, or snapshot-manifest fingerprints.

This is offline supplied-record ingestion. It does not download, update, activate, or claim completeness of the CISA Vulnrichment feed.

Source reference: [CISA Vulnrichment repository](https://github.com/cisagov/vulnrichment).
