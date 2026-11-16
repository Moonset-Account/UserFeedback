# V5o — Strict CPE parsing and explicit mapping

CPE 2.3 bindings are parsed into named components without inferring a PURL.

PURL mappings require:

- valid CPE 2.3 binding;
- `pkg:` PURL;
- confidence level;
- operator approval;
- non-empty rationale.

Stored mappings are marked `source=operator`. No automatic vendor/product mapping is performed.
