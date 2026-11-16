# V5r — Strict NVD validation and revision-safe normalization

## Validation contract

NVD nested fields fail closed when present with the wrong type:

- `descriptions`: list of objects with string `value`;
- CVSS metric families: lists of objects with `cvssData` objects;
- `weaknesses`: list with description lists and string values;
- `configurations`: list of nodes with `cpeMatch` lists and string criteria.

Malformed JSON is returned as an import error rather than escaping as a decoder exception.

## Revision contract

Each normalized NVD child row stores its parent `enrichment_digest`. Reports select exactly one active revision:

1. latest non-empty NVD `modified` timestamp;
2. digest descending as deterministic tie-breaker.

The report exposes both `revision_digest` and `revision_modified`. Historical rows are not mixed into the active normalized report.

Legacy normalized rows without a revision digest are intentionally not guessed into a revision; they require re-import to become revision-bound.
