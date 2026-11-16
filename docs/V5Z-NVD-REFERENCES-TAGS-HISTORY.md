# V5z — NVD references, tags, and change history

The offline NVD parser now preserves three metadata families without network access:

- `references`: URL, optional source, and reference tags;
- `tags`: NVD `cveTags` normalized to source identifier plus tag list;
- `history`: NVD CVE change events, including event metadata and individual changes.

The parser validates list/object/string shapes and fails closed on malformed nested payloads. Change history is accepted from either the top-level `cveChanges` field used by the Change History API or a nested `cve.cveChanges` field.

The normalized SQLite report exposes `references`, `tags`, and `history` alongside the existing CVSS, weakness, configuration, and flat CPE projections. The raw source record remains available in the stored enrichment revision, with deterministic content digests selecting the active revision.
